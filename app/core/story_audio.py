"""Generate TTS for a story and store in Supabase. Used by voice API and by deepen flow."""

import io
import logging
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone

from app.core.config import settings
from app.core.elevenlabs import text_to_speech
from app.core.supabase_client import get_supabase

try:
    from mutagen import File as MutagenFile
except ImportError:
    MutagenFile = None

MAX_SENTENCE_WORDS = 20
SENTENCES_PER_PARAGRAPH = (3, 4)


def _format_text_for_tts(text: str) -> str:
    """
    Prepare story text for natural-sounding TTS:
    - Normalize em dashes and ellipses (ElevenLabs reads them as natural pauses)
    - Insert commas after common introductory words for breathing room
    - Break overly long sentences at conjunctions
    - Group sentences into paragraphs every 3-4 sentences
    """
    if not text or not text.strip():
        return text

    s = text.strip()

    # Strip any SSML / break tags (including escaped-quote variants from old runs)
    s = re.sub(r"<\s*break\b[^>]*?/?\s*>", "", s)
    s = re.sub(r"<\s*/?\s*speak\s*>", "", s)
    s = re.sub(r'<break\s+time\s*=\s*\\?"[^"]*\\?"\s*/?\s*>', "", s)
    s = re.sub(r"\s+", " ", s).strip()

    # Normalize double-dashes to em dash
    s = re.sub(r"\s*--\s*", " — ", s)
    # Normalize em dash spacing
    s = re.sub(r"\s*—\s*", " — ", s)
    # Normalize ellipses and protect them from sentence splitting
    ELLIPSIS_PLACEHOLDER = "\x01ELLIPSIS\x01"
    s = re.sub(r"\.{3,}", ELLIPSIS_PLACEHOLDER, s)

    # Split into sentences
    parts = re.split(r"([.!?])\s*", s)
    sentences: list[str] = []
    current = ""
    for part in parts:
        if re.match(r"^[.!?]$", part):
            current = (current + part).strip()
            if current:
                sentences.append(current)
            current = ""
        else:
            current = (current + part).strip()
    if current.strip():
        sentences.append(current.strip())

    # Restore ellipses
    sentences = [sent.replace(ELLIPSIS_PLACEHOLDER, "...") for sent in sentences]

    # Break long sentences at conjunctions
    conj = re.compile(r"\s+(and|but|so|or|then|yet|nor)\s+", re.I)
    result: list[str] = []
    for sent in sentences:
        if len(sent.split()) <= MAX_SENTENCE_WORDS:
            result.append(sent)
            continue
        remaining = sent
        while remaining.strip():
            remaining = remaining.strip()
            found = False
            for m in conj.finditer(remaining):
                prefix = remaining[: m.start()].strip()
                wc = len(prefix.split())
                if 5 <= wc <= MAX_SENTENCE_WORDS:
                    result.append(prefix + " " + m.group(0).strip())
                    remaining = remaining[m.end() :].strip()
                    found = True
                    break
            if not found:
                wrds = remaining.split()
                if len(wrds) <= MAX_SENTENCE_WORDS:
                    result.append(remaining)
                    break
                chunk = " ".join(wrds[:MAX_SENTENCE_WORDS])
                remaining = " ".join(wrds[MAX_SENTENCE_WORDS:]).strip()
                result.append(chunk)

    # Insert commas after introductory words when missing
    intro = re.compile(
        r"^(Well|So|However|First|Then|Now|Yes|No|Actually|Finally|Suddenly)\s+(?!,)",
        re.I,
    )
    for i, sent in enumerate(result):
        m = intro.match(sent)
        if m:
            word = m.group(1)
            result[i] = word + ", " + sent[m.end() :]

    # Ensure every sentence ends with punctuation
    final: list[str] = []
    for sent in result:
        sent = sent.strip()
        if sent and sent[-1] not in ".!?":
            sent += "."
        final.append(sent)

    # Group into paragraphs (separated by blank lines)
    paragraphs: list[str] = []
    i = 0
    while i < len(final):
        left = len(final) - i
        take = min(SENTENCES_PER_PARAGRAPH[1], left) if left >= SENTENCES_PER_PARAGRAPH[0] else left
        take = max(1, take)
        paragraphs.append(" ".join(final[i : i + take]))
        i += take
    return "\n\n".join(paragraphs)


# ElevenLabs SSML pauses (max 3s per break). Reference: natural narration pacing.
PAUSE_COMMA = '<break time="0.2s" />'  # quick breath between clauses
PAUSE_ELLIPSIS = '<break time="0.5s" />'  # brief dramatic pause
PAUSE_COLON = '<break time="0.2s" />'  # short clause setup pause
PAUSE_SENTENCE = '<break time="0.7s" />'  # end of thought, listener processes
PAUSE_PARAGRAPH = '<break time="0s" />'  # scene/topic change (ElevenLabs max)


def _add_breaks_to_paragraph(paragraph: str, *, add_trailing_paragraph_break: bool = False) -> str:
    """
    Add SSML break tags per punctuation. Chunk per paragraph to limit tag count.
    No trailing break between chunks so playback continues with 0s gap.
    """
    parts: list[str] = []
    length = len(paragraph)
    i = 0
    while i < length:
        ch = paragraph[i]
        if ch == "." and i + 2 < length and paragraph[i + 1] == "." and paragraph[i + 2] == ".":
            parts.append("...")
            i += 3
            parts.append(f" {PAUSE_ELLIPSIS}")
            continue
        parts.append(ch)
        nxt = paragraph[i + 1] if i + 1 < length else ""
        if ch in ".!?":
            if nxt == " " or nxt == "":
                parts.append(f" {PAUSE_SENTENCE}")
        elif ch == ":" and nxt == " ":
            parts.append(f" {PAUSE_COLON}")
        elif ch == "," and nxt == " ":
            parts.append(f" {PAUSE_COMMA}")
        i += 1
    result = "".join(parts)
    tail = f" {PAUSE_PARAGRAPH}" if add_trailing_paragraph_break else ""
    return f"<speak>{result}{tail}</speak>"


async def generate_and_store_story_audio(
    *,
    story_id: int,
    voice_id: str,
    text: str | None = None,
    model_id: str = "eleven_multilingual_v2",
) -> dict | None:
    """
    Generate TTS for a story and store in Supabase. If text is not provided, load from Stories by story_id.
    Generates audio per paragraph to keep SSML break count low (avoids speed-up
    artifact), then concatenates the MP3 chunks. Uses previous_text/next_text
    for natural voice continuity between paragraphs.
    Returns {"url": ..., "content_type": ...} or None if story not found / no text / upload skipped.
    """
    logging.info("Generate the voice")
    if not text or not text.strip():
        supabase = get_supabase()
        r = supabase.table("Stories").select("story").eq("id", story_id).or_("is_deleted.eq.false,is_deleted.is.null").execute()
        rows = r.data or []
        row = rows[0] if rows else {}
        text = (row.get("story") or row.get("Story") or "").strip()
    if not text:
        return None

    formatted_text = _format_text_for_tts(text)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", formatted_text) if p.strip()]
    logging.debug("[TTS] formatted text (full):\n%s", formatted_text)
    logging.debug("[TTS] total chunks: %d", len(paragraphs))
    audio_chunks: list[bytes] = []
    content_type = "audio/mpeg"
    total_duration = 0.0

    for idx, para in enumerate(paragraphs):
        # No trailing break between chunks so playback continues with 0s gap
        ssml_chunk = _add_breaks_to_paragraph(para, add_trailing_paragraph_break=False)
        prev_text = paragraphs[idx - 1] if idx > 0 else None
        next_text = paragraphs[idx + 1] if idx < len(paragraphs) - 1 else None

        logging.debug("[TTS] chunk %d/%d paragraph:\n%s", idx + 1, len(paragraphs), para)
        logging.debug(
            "[TTS] chunk %d/%d SSML (len=%d):\n%s",
            idx + 1,
            len(paragraphs),
            len(ssml_chunk),
            ssml_chunk,
        )

        chunk_bytes, ct = await text_to_speech(
            voice_id=voice_id,
            text=ssml_chunk,
            model_id=model_id,
            enable_ssml=True,
            previous_text=prev_text,
            next_text=next_text,
        )
        audio_chunks.append(chunk_bytes)
        content_type = ct

        if MutagenFile:
            try:
                af = MutagenFile(io.BytesIO(chunk_bytes))
                if af and hasattr(af, "info") and af.info:
                    total_duration += af.info.length
            except Exception as e:
                logging.warning("Could not get chunk %d duration: %s", idx + 1, e)

    audio_bytes = b"".join(audio_chunks)
    play_length = round(total_duration, 2) if total_duration > 0 else None

    public_url = None
    if settings.SUPABASE_URL and settings.SUPABASE_KEY:
        bucket = settings.SUPABASE_STORAGE_BUCKET
        ext = "mp3" if "mpeg" in content_type or "mp3" in content_type else "mp4"
        path = f"{voice_id}/{uuid.uuid4().hex}.{ext}"
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
                tmp.write(audio_bytes)
                tmp_path = tmp.name
            try:
                supabase.storage.from_(bucket).upload(
                    path,
                    tmp_path,
                    file_options={"contentType": str(content_type), "upsert": "true"},
                )
                public_url = supabase.storage.from_(bucket).get_public_url(path)
                update_payload = {"storage": path, "playUrl": public_url, "last_played": now_iso, "voice_id": voice_id}
                if play_length is not None:
                    update_payload["play_length"] = play_length
                supabase.table("Stories").update(update_payload).eq("id", story_id).execute()
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except Exception as e:
            logging.exception("Supabase storage upload failed for story %s: %s", story_id, e)

    return {"url": public_url, "content_type": content_type}
    # return {"format_text": formatted_text, "text_with_breaks": text_with_breaks}

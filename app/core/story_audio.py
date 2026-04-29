"""Generate TTS for a story and store in Supabase. Used by voice API and by deepen flow."""

from __future__ import annotations

import io
import json
import logging
import os
import re
import tempfile
import time
import uuid
import wave
from datetime import datetime, timezone

from app.core.auphonic import AuphonicError, process_audio as process_audio_with_auphonic
from app.core.config import settings
from app.core.db_utils import safe_partial_update
from app.core.elevenlabs import default_voice_settings, text_to_speech
from app.core.story_text import ensure_complete_story_text, prepare_story_for_narration
from app.core.supabase_client import get_supabase

try:
    from mutagen import File as MutagenFile
except ImportError:
    MutagenFile = None

try:
    import httpx
except ImportError:
    httpx = None

MAX_SENTENCE_WORDS = 20
SENTENCES_PER_PARAGRAPH = (6, 8)
MAX_TTS_CHUNK_CHARS = 1800
PCM_SAMPLE_WIDTH_BYTES = 2
PCM_JOIN_SILENCE_MS = 120
PCM_EDGE_FADE_MS = 8
PCM_EDGE_TRIM_MS = 90
PCM_TRIM_THRESHOLD = 64

# region agent log
_DEBUG_LOG_PATH = "/home/sebastian/Documents/Already/.cursor/debug-7a5035.log"
_DEBUG_SESSION_ID = "7a5035"


def _agent_log(*, hypothesis_id: str, location: str, message: str, data: dict) -> None:
    try:
        payload = {
            "sessionId": _DEBUG_SESSION_ID,
            "runId": "pre-fix",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        # Always print so it's visible regardless of logging config.
        try:
            print(
                f"[agentlog] {hypothesis_id} {location} {message} data={payload.get('data')}",
                flush=True,
            )
        except Exception:
            pass
        # Also mirror to server logs (if configured).
        logging.info(
            "[agentlog] %s %s %s data=%s",
            hypothesis_id,
            location,
            message,
            payload.get("data"),
        )
        try:
            os.makedirs(os.path.dirname(_DEBUG_LOG_PATH), exist_ok=True)
        except Exception:
            pass
        with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass

# endregion agent log


def _format_text_for_tts(text: str) -> str:
    """
    Prepare story text for natural-sounding TTS:
    - Normalize em dashes and ellipses (ElevenLabs reads them as natural pauses)
    - Insert commas after common introductory words for breathing room
    - Break overly long sentences at conjunctions
    - Group sentences into larger chunks to reduce TTS request boundaries
    """
    if not text or not text.strip():
        return text

    s = text.strip()

    # Strip any SSML / break tags (including escaped-quote variants from old runs)
    s = re.sub(r"<\s*break\b[^>]*?/?\s*>", "", s)
    s = re.sub(r"<\s*/?\s*speak\s*>", "", s)
    s = re.sub(r'<break\s+time\s*=\s*\\?"[^"]*\\?"\s*/?\s*>', "", s)
    s = re.sub(r"\s+", " ", s).strip()

    # Prevent TTS from treating common title abbreviations as sentence breaks.
    # This also avoids SSML break insertion after these periods later.
    s = re.sub(r"\bMr\.\b", "Mister", s)
    s = re.sub(r"\bMrs\.\b", "Misses", s)
    s = re.sub(r"\bMs\.\b", "Miss", s)
    s = re.sub(r"\bDr\.\b", "Doctor", s)
    s = re.sub(r"\bSt\.\b", "Saint", s)

    # Normalize double-dashes to em dash
    s = re.sub(r"\s*--\s*", " — ", s)
    # Normalize em dash spacing
    s = re.sub(r"\s*—\s*", " — ", s)
    # Normalize ellipses and protect them from sentence splitting
    ellipsis_placeholder = "\x01ELLIPSIS\x01"
    s = re.sub(r"\.{3,}", ellipsis_placeholder, s)

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

    sentences = [sent.replace(ellipsis_placeholder, "...") for sent in sentences]

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
            for match in conj.finditer(remaining):
                prefix = remaining[: match.start()].strip()
                wc = len(prefix.split())
                if 5 <= wc <= MAX_SENTENCE_WORDS:
                    result.append(prefix + " " + match.group(0).strip())
                    remaining = remaining[match.end() :].strip()
                    found = True
                    break
            if not found:
                words = remaining.split()
                if len(words) <= MAX_SENTENCE_WORDS:
                    result.append(remaining)
                    break
                chunk = " ".join(words[:MAX_SENTENCE_WORDS])
                remaining = " ".join(words[MAX_SENTENCE_WORDS:]).strip()
                result.append(chunk)

    intro = re.compile(
        r"^(Well|So|However|First|Then|Now|Yes|Actually|Finally|Suddenly)\s+(?!,)",
        re.I,
    )
    for idx, sent in enumerate(result):
        match = intro.match(sent)
        if match:
            word = match.group(1)
            result[idx] = word + ", " + sent[match.end() :]

    final: list[str] = []
    for sent in result:
        sent = sent.strip()
        if sent and sent[-1] not in ".!?":
            sent += "."
        final.append(sent)

    paragraphs: list[str] = []
    current: list[str] = []
    current_chars = 0
    min_sentences, max_sentences = SENTENCES_PER_PARAGRAPH
    for sent in final:
        added_chars = len(sent) + (1 if current else 0)
        would_exceed_sentences = len(current) >= max_sentences
        would_exceed_chars = current_chars + added_chars > MAX_TTS_CHUNK_CHARS
        if current and (would_exceed_sentences or (len(current) >= min_sentences and would_exceed_chars)):
            paragraphs.append(" ".join(current))
            current = []
            current_chars = 0

        current.append(sent)
        current_chars += len(sent) + (1 if current_chars else 0)

    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs)


PAUSE_COMMA = '<break time="0.2s" />'
PAUSE_ELLIPSIS = '<break time="0.5s" />'
PAUSE_COLON = '<break time="0.2s" />'
PAUSE_PARAGRAPH = '<break time="0s" />'


def _add_breaks_to_paragraph(paragraph: str, *, add_trailing_paragraph_break: bool = False) -> str:
    """Add SSML break tags per punctuation."""
    parts: list[str] = []
    length = len(paragraph)
    idx = 0
    while idx < length:
        ch = paragraph[idx]
        if ch == "." and idx + 2 < length and paragraph[idx + 1] == "." and paragraph[idx + 2] == ".":
            parts.append("...")
            idx += 3
            parts.append(f" {PAUSE_ELLIPSIS}")
            continue
        parts.append(ch)
        nxt = paragraph[idx + 1] if idx + 1 < length else ""
        if ch == ":" and nxt == " ":
            parts.append(f" {PAUSE_COLON}")
        elif ch == "," and nxt == " ":
            parts.append(f" {PAUSE_COMMA}")
        idx += 1
    result = "".join(parts)
    tail = f" {PAUSE_PARAGRAPH}" if add_trailing_paragraph_break else ""
    return f"<speak>{result}{tail}</speak>"


def _parse_output_format(output_format: str) -> tuple[str, int]:
    parts = (output_format or "").split("_")
    if len(parts) < 2:
        raise ValueError(f"Invalid ElevenLabs output format: {output_format!r}")
    codec = parts[0]
    sample_rate = int(parts[1])
    return codec, sample_rate


def _pcm_sample_at(pcm: bytes | bytearray, index: int) -> int:
    start = index * PCM_SAMPLE_WIDTH_BYTES
    return int.from_bytes(pcm[start : start + PCM_SAMPLE_WIDTH_BYTES], byteorder="little", signed=True)


def _set_pcm_sample(pcm: bytearray, index: int, value: int) -> None:
    start = index * PCM_SAMPLE_WIDTH_BYTES
    value = max(-32768, min(32767, int(value)))
    pcm[start : start + PCM_SAMPLE_WIDTH_BYTES] = value.to_bytes(
        PCM_SAMPLE_WIDTH_BYTES,
        byteorder="little",
        signed=True,
    )


def _trim_pcm_chunk_edges(audio: bytes, sample_rate: int) -> bytes:
    """Remove low-level edge residue from TTS chunks without trimming speech body."""
    usable_length = len(audio) - (len(audio) % PCM_SAMPLE_WIDTH_BYTES)
    audio = audio[:usable_length]
    frame_count = usable_length // PCM_SAMPLE_WIDTH_BYTES
    edge_frames = int(sample_rate * PCM_EDGE_TRIM_MS / 1000)
    if frame_count == 0 or frame_count <= edge_frames * 2:
        return audio

    start_frame = 0
    start_scan_end = min(edge_frames, frame_count)
    while start_frame < start_scan_end and abs(_pcm_sample_at(audio, start_frame)) <= PCM_TRIM_THRESHOLD:
        start_frame += 1
    if start_frame >= start_scan_end:
        start_frame = 0

    end_frame = frame_count
    end_scan_start = max(0, frame_count - edge_frames)
    cursor = frame_count - 1
    while cursor >= end_scan_start and abs(_pcm_sample_at(audio, cursor)) <= PCM_TRIM_THRESHOLD:
        cursor -= 1
    if cursor >= end_scan_start:
        end_frame = cursor + 1

    if start_frame >= end_frame:
        return audio
    return audio[start_frame * PCM_SAMPLE_WIDTH_BYTES : end_frame * PCM_SAMPLE_WIDTH_BYTES]


def _fade_pcm_chunk_edges(audio: bytes, sample_rate: int) -> bytes:
    frame_count = len(audio) // PCM_SAMPLE_WIDTH_BYTES
    fade_frames = int(sample_rate * PCM_EDGE_FADE_MS / 1000)
    if frame_count == 0 or fade_frames <= 0 or frame_count <= fade_frames * 4:
        return audio

    faded = bytearray(audio)
    for index in range(fade_frames):
        fade_in_scale = (index + 1) / fade_frames
        fade_out_scale = (fade_frames - index) / fade_frames
        _set_pcm_sample(faded, index, round(_pcm_sample_at(faded, index) * fade_in_scale))
        end_index = frame_count - fade_frames + index
        _set_pcm_sample(faded, end_index, round(_pcm_sample_at(faded, end_index) * fade_out_scale))
    return bytes(faded)


def _join_pcm_chunks(audio_chunks: list[bytes], sample_rate: int) -> bytes:
    cleaned_chunks: list[bytes] = []
    for chunk in audio_chunks:
        cleaned = _trim_pcm_chunk_edges(chunk, sample_rate)
        if cleaned:
            cleaned_chunks.append(_fade_pcm_chunk_edges(cleaned, sample_rate))

    if not cleaned_chunks:
        return b""
    if len(cleaned_chunks) == 1:
        return cleaned_chunks[0]

    silence_frames = int(sample_rate * PCM_JOIN_SILENCE_MS / 1000)
    join_silence = b"\x00" * (silence_frames * PCM_SAMPLE_WIDTH_BYTES)
    return join_silence.join(cleaned_chunks)


def _pcm_chunks_to_wav(audio_chunks: list[bytes], sample_rate: int) -> bytes:
    raw_audio = _join_pcm_chunks(audio_chunks, sample_rate)
    with io.BytesIO() as buffer:
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(raw_audio)
        return buffer.getvalue()


def _wav_chunks_to_wav(audio_chunks: list[bytes]) -> tuple[bytes, float]:
    params = None
    frames: list[bytes] = []
    total_duration = 0.0
    for chunk in audio_chunks:
        with wave.open(io.BytesIO(chunk), "rb") as wav_file:
            current_params = (
                wav_file.getnchannels(),
                wav_file.getsampwidth(),
                wav_file.getframerate(),
                wav_file.getcomptype(),
                wav_file.getcompname(),
            )
            if params is None:
                params = current_params
            elif current_params != params:
                raise ValueError("WAV chunks used different audio params and cannot be merged safely")
            frame_count = wav_file.getnframes()
            total_duration += frame_count / float(wav_file.getframerate())
            frames.append(wav_file.readframes(frame_count))

    if params is None:
        return b"", 0.0

    with io.BytesIO() as buffer:
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(params[0])
            wav_file.setsampwidth(params[1])
            wav_file.setframerate(params[2])
            wav_file.setcomptype(params[3], params[4])
            wav_file.writeframes(b"".join(frames))
        return buffer.getvalue(), total_duration


def _extension_for_content_type(content_type: str) -> str:
    content_type = (content_type or "").lower()
    if "wav" in content_type:
        return "wav"
    if "mpeg" in content_type or "mp3" in content_type:
        return "mp3"
    if "mp4" in content_type or "aac" in content_type:
        return "m4a"
    return "bin"


def _estimate_duration(content_type: str, output_format: str, audio_bytes: bytes) -> float | None:
    content_type = (content_type or "").lower()
    if "wav" in content_type:
        try:
            with wave.open(io.BytesIO(audio_bytes), "rb") as wav_file:
                return wav_file.getnframes() / float(wav_file.getframerate())
        except Exception:
            logging.debug("Wave parser could not estimate duration", exc_info=True)

    try:
        codec, sample_rate = _parse_output_format(output_format)
    except ValueError:
        codec, sample_rate = "", 0

    if codec == "pcm" and sample_rate:
        return len(audio_bytes) / float(sample_rate * 2)

    if codec == "wav":
        with wave.open(io.BytesIO(audio_bytes), "rb") as wav_file:
            return wav_file.getnframes() / float(wav_file.getframerate())

    if MutagenFile:
        try:
            audio_file = MutagenFile(io.BytesIO(audio_bytes))
            if audio_file and getattr(audio_file, "info", None):
                return float(audio_file.info.length)
        except Exception:
            logging.debug("Mutagen could not estimate %s duration", content_type, exc_info=True)
    return None


def _write_temp_file(content: bytes, suffix: str) -> str:
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        return tmp.name


def _upload_temp_file(*, bucket: str, path: str, tmp_path: str, content_type: str) -> str:
    supabase = get_supabase()
    supabase.storage.from_(bucket).upload(
        path,
        tmp_path,
        file_options={"contentType": str(content_type), "upsert": "true"},
    )
    return supabase.storage.from_(bucket).get_public_url(path)


def _store_manifest(*, bucket: str, manifest_path: str, manifest: dict) -> str:
    tmp_path = _write_temp_file(json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"), ".json")
    try:
        return _upload_temp_file(
            bucket=bucket,
            path=manifest_path,
            tmp_path=tmp_path,
            content_type="application/json",
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def generate_and_store_story_audio(
    *,
    story_id: int,
    voice_id: str,
    text: str | None = None,
    model_id: str = "eleven_multilingual_v2",
    voice_settings: dict | None = None,
    output_format: str | None = None,
    seed: int | None = None,
    apply_postprocess: bool = True,
) -> dict | None:
    """
    Generate TTS for a story and store it in Supabase.

    The default flow requests PCM from ElevenLabs, concatenates raw frames safely,
    and wraps the final result into a single WAV file to avoid MP3 stitch artifacts.
    """
    generation_started_at = time.perf_counter()
    logging.info("Generate story audio for story_id=%s voice_id=%s", story_id, voice_id)
    _agent_log(
        hypothesis_id="A",
        location="app/core/story_audio.py:generate_and_store_story_audio:start",
        message="audio generation start",
        data={
            "storyId": story_id,
            "hasVoiceId": bool((voice_id or "").strip()),
            "modelId": model_id,
            "hasTextOverride": bool((text or "").strip()),
            "hasVoiceSettings": voice_settings is not None,
            "outputFormat": output_format,
            "seedProvided": seed is not None,
            "auphonicEnabled": bool(settings.AUPHONIC_ENABLED),
            "applyPostprocess": bool(apply_postprocess),
        },
    )
    if not text or not text.strip():
        supabase = get_supabase()
        response = supabase.table("Stories").select("story").eq("id", story_id).or_("is_deleted.eq.false,is_deleted.is.null").execute()
        rows = response.data or []
        row = rows[0] if rows else {}
        text = (row.get("story") or row.get("Story") or "").strip()
    if not text:
        return None

    narration_script = prepare_story_for_narration(text)
    narration_script, story_meta = ensure_complete_story_text(narration_script)
    formatted_text = _format_text_for_tts(narration_script)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", formatted_text) if p.strip()]
    if not paragraphs:
        return None
    _agent_log(
        hypothesis_id="D",
        location="app/core/story_audio.py:generate_and_store_story_audio:chunks",
        message="prepared TTS chunks",
        data={
            "storyId": story_id,
            "chunkCount": len(paragraphs),
            "totalChars": len(narration_script),
        },
    )

    selected_output_format = output_format or settings.ELEVENLABS_TTS_OUTPUT_FORMAT
    request_voice_settings = default_voice_settings()
    if voice_settings:
        request_voice_settings.update({k: v for k, v in voice_settings.items() if v is not None})
    request_speed = request_voice_settings.get("speed")
    request_voice_settings = {k: v for k, v in request_voice_settings.items() if v is not None}

    try:
        safe_partial_update(
            table_name="Stories",
            id_field="id",
            record_id=story_id,
            optional_payload={
                "audio_generation_status": "started",
                "audio_generation_error": None,
                "audio_script": narration_script,
                "audio_chunk_count": len(paragraphs),
                "audio_model_id": model_id,
                "audio_output_format": selected_output_format,
                "audio_voice_settings": request_voice_settings,
            },
        )
    except Exception:
        logging.exception("Failed to persist initial audio metadata for story %s", story_id)

    logging.debug("[TTS] formatted text (full):\n%s", formatted_text)
    logging.debug("[TTS] total chunks: %d", len(paragraphs))

    audio_chunks: list[bytes] = []
    chunk_traces: list[dict] = []
    request_ids: list[str] = []
    continuity_request_ids: list[str] = []
    total_duration = 0.0
    final_content_type = "application/octet-stream"
    tts_started_at = time.perf_counter()

    try:
        for idx, paragraph in enumerate(paragraphs):
            ssml_chunk = _add_breaks_to_paragraph(paragraph, add_trailing_paragraph_break=False)
            prev_text = paragraphs[idx - 1] if idx > 0 else None
            next_text = paragraphs[idx + 1] if idx < len(paragraphs) - 1 else None
            started_at = time.perf_counter()
            result = await text_to_speech(
                voice_id=voice_id,
                text=ssml_chunk,
                model_id=model_id,
                output_format=selected_output_format,
                voice_settings=request_voice_settings,
                enable_ssml=True,
                previous_text=prev_text,
                next_text=next_text,
                previous_request_ids=continuity_request_ids[-3:] or None,
                speed=request_speed,
                seed=seed,
            )
            latency_ms = round((time.perf_counter() - started_at) * 1000, 2)
            request_id = result.request_id or f"chunk-{idx + 1}"
            request_ids.append(request_id)
            if result.request_id:
                continuity_request_ids.append(result.request_id)
            audio_chunks.append(result.audio_bytes)
            final_content_type = result.content_type

            duration = _estimate_duration(result.content_type, result.output_format, result.audio_bytes)
            if duration is not None:
                total_duration += duration

            chunk_traces.append(
                {
                    "index": idx + 1,
                    "request_id": result.request_id,
                    "history_item_id": result.history_item_id,
                    "chars": len(paragraph),
                    "ssml_chars": len(ssml_chunk),
                    "response_bytes": len(result.audio_bytes),
                    "latency_ms": latency_ms,
                    "duration_seconds": round(duration, 3) if duration is not None else None,
                    "output_format": result.output_format,
                }
            )
    except Exception as exc:
        if httpx is not None and isinstance(exc, httpx.HTTPStatusError):
            try:
                status_code = exc.response.status_code if exc.response is not None else None
                response_text = (exc.response.text or "") if exc.response is not None else ""
            except Exception:
                status_code = None
                response_text = ""
            response_text = response_text[:1500]
            logging.warning(
                "[TTS] ElevenLabs HTTPStatusError story_id=%s status=%s body=%s",
                story_id,
                status_code,
                response_text,
            )
        _agent_log(
            hypothesis_id="D",
            location="app/core/story_audio.py:generate_and_store_story_audio:tts_exception",
            message="TTS loop exception",
            data={
                "storyId": story_id,
                "elapsedMs": round((time.perf_counter() - generation_started_at) * 1000, 2),
                "errorType": type(exc).__name__,
            },
        )
        try:
            safe_partial_update(
                table_name="Stories",
                id_field="id",
                record_id=story_id,
                optional_payload={
                    "audio_generation_status": "failed",
                    "audio_generation_error": str(exc),
                    "audio_script": narration_script,
                },
            )
        except Exception:
            logging.exception("Failed to persist audio failure metadata for story %s", story_id)
        raise

    codec, sample_rate = _parse_output_format(selected_output_format)
    if codec == "pcm":
        audio_bytes = _pcm_chunks_to_wav(audio_chunks, sample_rate)
        final_content_type = "audio/wav"
        joined_duration = _estimate_duration(final_content_type, "wav", audio_bytes)
        if joined_duration is not None:
            total_duration = joined_duration
        file_ext = "wav"
    elif codec == "wav":
        audio_bytes, wav_duration = _wav_chunks_to_wav(audio_chunks)
        final_content_type = "audio/wav"
        total_duration = total_duration or wav_duration
        file_ext = "wav"
    else:
        audio_bytes = b"".join(audio_chunks)
        file_ext = _extension_for_content_type(final_content_type)

    tts_processing_ms = round((time.perf_counter() - tts_started_at) * 1000, 2)
    _agent_log(
        hypothesis_id="D",
        location="app/core/story_audio.py:generate_and_store_story_audio:tts_done",
        message="TTS done (all chunks)",
        data={
            "storyId": story_id,
            "ttsProcessingMs": tts_processing_ms,
            "bytesBeforePostprocess": len(audio_bytes) if "audio_bytes" in locals() else None,
            "finalContentTypeBeforePostprocess": final_content_type,
            "chunkCount": len(paragraphs),
        },
    )
    play_length = round(total_duration, 2) if total_duration > 0 else None
    postprocess_metadata = {
        "provider": "auphonic",
        "enabled": bool(settings.AUPHONIC_ENABLED),
        "requested": bool(apply_postprocess),
        "applied": False,
        "fallback_reason": None,
    }
    if settings.AUPHONIC_ENABLED and apply_postprocess:
        try:
            auphonic_result = await process_audio_with_auphonic(
                audio_bytes=audio_bytes,
                filename=f"story-{story_id}.{file_ext}",
                title=f"Story {story_id}",
                output_basename=f"story-{story_id}",
            )
            audio_bytes = auphonic_result.audio_bytes
            final_content_type = auphonic_result.content_type
            file_ext = auphonic_result.file_ext
            processed_duration = _estimate_duration(
                final_content_type,
                auphonic_result.output_format,
                audio_bytes,
            )
            if processed_duration is not None:
                play_length = round(processed_duration, 2)
            postprocess_metadata = {
                **postprocess_metadata,
                **auphonic_result.metadata,
                "applied": True,
                "final_content_type": final_content_type,
                "final_file_ext": file_ext,
                "play_length": play_length,
            }
        except AuphonicError as exc:
            logging.warning(
                "Auphonic post-processing failed for story %s, falling back to ElevenLabs audio: %s",
                story_id,
                exc,
            )
            postprocess_metadata = {
                **postprocess_metadata,
                "error": str(exc),
                "fallback_reason": str(exc),
            }
    _agent_log(
        hypothesis_id="B",
        location="app/core/story_audio.py:generate_and_store_story_audio:postprocess_done",
        message="postprocess done (auphonic or skipped)",
        data={
            "storyId": story_id,
            "applied": bool(postprocess_metadata.get("applied")),
            "postprocessMs": postprocess_metadata.get("processing_ms"),
            "finalContentType": final_content_type,
            "finalBytes": len(audio_bytes),
        },
    )

    processing_metrics = {
        "tts_processing_ms": tts_processing_ms,
        "postprocess_ms": postprocess_metadata.get("processing_ms"),
        "processing_until_manifest_ms": round(
            (time.perf_counter() - generation_started_at) * 1000,
            2,
        ),
    }
    if postprocess_metadata.get("applied"):
        processing_metrics["postprocess_provider"] = "auphonic"

    manifest = {
        "story_id": story_id,
        "voice_id": voice_id,
        "model_id": model_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "script": narration_script,
        "formatted_text": formatted_text,
        "story_text_checks": story_meta,
        "voice_settings": request_voice_settings,
        "output_format": selected_output_format,
        "chunk_count": len(paragraphs),
        "chunk_traces": chunk_traces,
        "request_ids": [rid for rid in request_ids if rid and not rid.startswith("chunk-")],
        "play_length": play_length,
        "content_type": final_content_type,
        "postprocess": postprocess_metadata,
        "metrics": processing_metrics,
    }

    public_url = None
    manifest_url = None
    storage_path = None
    manifest_path = None
    total_generation_ms = None
    if settings.SUPABASE_URL and settings.SUPABASE_KEY:
        bucket = settings.SUPABASE_STORAGE_BUCKET
        now_iso = datetime.now(timezone.utc).isoformat()
        storage_path = f"{voice_id}/{uuid.uuid4().hex}.{file_ext}"
        manifest_path = f"{voice_id}/{uuid.uuid4().hex}.json"
        tmp_path = None
        try:
            tmp_path = _write_temp_file(audio_bytes, f".{file_ext}")
            public_url = _upload_temp_file(
                bucket=bucket,
                path=storage_path,
                tmp_path=tmp_path,
                content_type=final_content_type,
            )
            manifest_url = _store_manifest(bucket=bucket, manifest_path=manifest_path, manifest=manifest)
            base_payload = {
                "storage": storage_path,
                "playUrl": public_url,
                "last_played": now_iso,
                "voice_id": voice_id,
            }
            if play_length is not None:
                base_payload["play_length"] = play_length
            total_generation_ms = round((time.perf_counter() - generation_started_at) * 1000, 2)
            safe_partial_update(
                table_name="Stories",
                id_field="id",
                record_id=story_id,
                base_payload=base_payload,
                optional_payload={
                    "audio_generation_status": "completed",
                    "audio_generation_error": None,
                    "audio_script": narration_script,
                    "audio_chunk_count": len(paragraphs),
                    "audio_model_id": model_id,
                    "audio_output_format": selected_output_format,
                    "audio_voice_settings": request_voice_settings,
                    "audio_manifest_path": manifest_path,
                    "audio_manifest_url": manifest_url,
                    "audio_tts_processing_ms": tts_processing_ms,
                    "audio_postprocess_provider": "auphonic" if postprocess_metadata.get("applied") else None,
                    "audio_postprocess_status": (
                        "completed"
                        if postprocess_metadata.get("applied")
                        else ("disabled" if not postprocess_metadata.get("enabled") else "fallback")
                    ),
                    "audio_postprocess_metadata": postprocess_metadata,
                    "audio_postprocess_ms": postprocess_metadata.get("processing_ms"),
                    "audio_generation_total_ms": total_generation_ms,
                },
            )
        except Exception as exc:
            _agent_log(
                hypothesis_id="C",
                location="app/core/story_audio.py:generate_and_store_story_audio:upload_exception",
                message="Supabase upload/update exception",
                data={
                    "storyId": story_id,
                    "elapsedMs": round((time.perf_counter() - generation_started_at) * 1000, 2),
                    "errorType": type(exc).__name__,
                },
            )
            logging.exception("Supabase storage upload failed for story %s: %s", story_id, exc)
            try:
                safe_partial_update(
                    table_name="Stories",
                    id_field="id",
                    record_id=story_id,
                    optional_payload={
                        "audio_generation_status": "failed",
                        "audio_generation_error": str(exc),
                        "audio_script": narration_script,
                    },
                )
            except Exception:
                logging.exception("Failed to persist upload failure metadata for story %s", story_id)
            raise
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    _agent_log(
        hypothesis_id="A",
        location="app/core/story_audio.py:generate_and_store_story_audio:done",
        message="audio generation done",
        data={
            "storyId": story_id,
            "totalMs": round((time.perf_counter() - generation_started_at) * 1000, 2),
            "hasUrl": bool((public_url or "").strip()) if public_url is not None else False,
            "contentType": final_content_type,
            "metrics": {
                **processing_metrics,
                "audio_generation_total_ms": total_generation_ms,
            },
        },
    )
    return {
        "url": public_url,
        "content_type": final_content_type,
        "manifest_url": manifest_url,
        "storage_path": storage_path,
        "play_length": play_length,
        "postprocess": postprocess_metadata,
        "metrics": {
            **processing_metrics,
            "audio_generation_total_ms": total_generation_ms,
        },
    }

import logging
import os
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.elevenlabs import add_voice, text_to_speech
from app.core.story_audio import generate_and_store_story_audio
from app.core.supabase_client import get_supabase

router = APIRouter(prefix="/voice", tags=["voice"])


def _raise_http_from_httpx(e: BaseException) -> None:
    if isinstance(e, httpx.HTTPStatusError):
        raise HTTPException(status_code=e.response.status_code, detail=f"ElevenLabs API error: {e.response.text}")
    raise HTTPException(status_code=502, detail="Voice service error")


# Folder in project root where clone voice uploads are stored for inspection
VOICE_CLONE_UPLOADS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "uploads", "voice_clone")


@router.post("/clone")
async def clone_voice(
    user_id: int = Form(...),
    name: str = Form(...),
    files: list[UploadFile] = File(...),
):
    """Create a voice clone from uploaded audio; returns ElevenLabs voice_id."""
    if not files:
        raise HTTPException(status_code=400, detail="At least one audio file is required")

    file_tuples: list[tuple[str, bytes, str]] = []
    os.makedirs(VOICE_CLONE_UPLOADS_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    for i, f in enumerate(files):
        ct = f.content_type or "application/octet-stream"
        if not ct.startswith("audio/"):
            raise HTTPException(status_code=400, detail=f"Invalid file type: {f.filename or 'unknown'}. Use audio (MP3, WAV, etc.).")
        content = await f.read()
        if not content:
            raise HTTPException(status_code=400, detail=f"File is empty: {f.filename or 'unknown'}.")
        file_tuples.append((f.filename or "audio", content, ct))
        # Store copy in project folder for inspection
        base = (f.filename or "audio").rsplit(".", 1)[0] if (f.filename or "").find(".") >= 0 else (f.filename or "audio")
        ext = (f.filename or "audio").rsplit(".", 1)[-1].lower() if (f.filename or "").find(".") >= 0 else "bin"
        if ext in ("mp3", "wav", "m4a", "ogg", "webm", "flac"):
            pass
        else:
            ext = "bin"
        save_name = f"user{user_id}_{ts}_{i}_{base}.{ext}"
        save_path = os.path.join(VOICE_CLONE_UPLOADS_DIR, save_name)
        try:
            with open(save_path, "wb") as out:
                out.write(content)
        except OSError as e:
            logging.warning("Could not save clone audio to %s: %s", save_path, e)

    try:
        return await add_voice(name=name, files=file_tuples, user_id=user_id, remove_background_noise= 'false')
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        _raise_http_from_httpx(e)


# Default sentence used to generate a voice preview from ElevenLabs
VOICE_PREVIEW_TEXT = "Hello, how are you?"


@router.get("/preview")
async def voice_preview(voice_id: str = Query(..., min_length=1, description="ElevenLabs voice_id")):
    """Generate and return a short audio preview for the given ElevenLabs voice_id."""
    try:
        audio_bytes, content_type = await text_to_speech(
            voice_id=voice_id,
            text=VOICE_PREVIEW_TEXT,
            model_id="eleven_multilingual_v2",
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        _raise_http_from_httpx(e)
    return Response(content=audio_bytes, media_type=content_type)


class SpeakRequest(BaseModel):
    voice_id: str = Field(..., min_length=1)
    story_id: int = Field(..., description="Story id; story text is read from Stories.story")
    model_id: str = Field(default="eleven_multilingual_v2")

@router.get("/speak/{story_id}")
async def get_story_play_url(story_id: int):
    """Return the playUrl for the given story_id. 404 if story not found or playUrl not set."""
    supabase = get_supabase()
    r = supabase.table("Stories").select("playUrl").eq("id", story_id).or_("is_deleted.eq.false,is_deleted.is.null").execute()
    rows = list(r.data or [])
    if not rows:
        raise HTTPException(status_code=404, detail="Story not found")
    row = rows[0]
    play_url = (row.get("playUrl") or "").strip()
    if not play_url:
        raise HTTPException(status_code=404, detail="Story has no play URL yet")
    return {"playUrl": play_url}


@router.post("/generate_audio")
async def speak(request: SpeakRequest):
    """Get story text from Stories by story_id; return existing playUrl if already played, else TTS, store, return URL."""
    try:
        result = await generate_and_store_story_audio(
            story_id=request.story_id,
            voice_id=request.voice_id,
            model_id=request.model_id,
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        _raise_http_from_httpx(e)
    if result is None:
        supabase = get_supabase()
        r = supabase.table("Stories").select("id", "story").eq("id", request.story_id).or_("is_deleted.eq.false,is_deleted.is.null").execute()
        rows = r.data or []
        raise HTTPException(
            status_code=404 if not rows else 400,
            detail="Story not found or has no story text",
        )
    return {
        "url": result["url"],
        "content_type": result["content_type"],
        "play_length": result.get("play_length"),
    }
    # return {"format_text": result["format_text"], "text_with_breaks": result["text_with_breaks"]}

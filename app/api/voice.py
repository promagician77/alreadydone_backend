import logging
import os
import shutil
import subprocess
import tempfile
import time
import wave
import math
import struct
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.db_utils import safe_partial_update
from app.core.elevenlabs import add_voice, text_to_speech
from app.core.story_audio import generate_and_store_story_audio
from app.core.supabase_client import get_supabase

try:
    from mutagen import File as MutagenFile
except ImportError:
    MutagenFile = None

router = APIRouter(prefix="/voice", tags=["voice"])


def _raise_http_from_httpx(e: BaseException) -> None:
    if isinstance(e, httpx.HTTPStatusError):
        raise HTTPException(status_code=e.response.status_code, detail=f"ElevenLabs API error: {e.response.text}")
    raise HTTPException(status_code=502, detail="Voice service error")


# Folder in project root where clone voice uploads are stored for inspection
VOICE_CLONE_UPLOADS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "uploads", "voice_clone")


def _get_audio_duration_seconds(content: bytes) -> float | None:
    if not MutagenFile:
        return None
    try:
        with tempfile.SpooledTemporaryFile() as handle:
            handle.write(content)
            handle.seek(0)
            audio_file = MutagenFile(handle)
            if audio_file and getattr(audio_file, "info", None):
                return float(audio_file.info.length)
    except Exception:
        logging.debug("Could not read clone audio duration", exc_info=True)
    return None


def _analyze_wav_bytes(content: bytes) -> dict:
    with tempfile.SpooledTemporaryFile() as handle:
        handle.write(content)
        handle.seek(0)
        with wave.open(handle, "rb") as wav_file:
            frames = wav_file.readframes(wav_file.getnframes())
            sample_width = wav_file.getsampwidth()
            if not frames or sample_width <= 0:
                return {}
            peak, rms_value = _pcm_peak_rms(frames, sample_width)
            max_possible = float((2 ** (8 * sample_width - 1)) - 1) if sample_width in (1, 2, 3, 4) else 0.0
            return {
                "channels": wav_file.getnchannels(),
                "sample_rate": wav_file.getframerate(),
                "sample_width": sample_width,
                "peak_ratio": round(peak / max_possible, 4) if max_possible else None,
                "rms_ratio": round(rms_value / max_possible, 4) if max_possible else None,
            }


def _pcm_peak_rms(pcm_frames: bytes, sample_width: int) -> tuple[int, int]:
    """
    Compute peak and RMS for little-endian signed PCM frames.

    Replaces `audioop.max` and `audioop.rms` for Python 3.13+ where `audioop` is removed.
    """
    if not pcm_frames:
        return 0, 0
    if sample_width == 1:
        # 8-bit PCM is usually unsigned in WAV; wave module returns raw bytes.
        # Convert to signed centered at 128.
        peak = 0
        acc = 0.0
        n = len(pcm_frames)
        for b in pcm_frames:
            v = int(b) - 128
            av = abs(v)
            if av > peak:
                peak = av
            acc += float(v * v)
        rms = int(math.sqrt(acc / n)) if n else 0
        return peak, rms

    if sample_width == 2:
        peak = 0
        acc = 0.0
        n = len(pcm_frames) // 2
        for (v,) in struct.iter_unpack("<h", pcm_frames[: n * 2]):
            av = abs(int(v))
            if av > peak:
                peak = av
            acc += float(v * v)
        rms = int(math.sqrt(acc / n)) if n else 0
        return peak, rms

    if sample_width == 3:
        peak = 0
        acc = 0.0
        n = len(pcm_frames) // 3
        for i in range(0, n * 3, 3):
            b0 = pcm_frames[i]
            b1 = pcm_frames[i + 1]
            b2 = pcm_frames[i + 2]
            v = b0 | (b1 << 8) | (b2 << 16)
            if v & 0x800000:
                v -= 0x1000000
            av = abs(v)
            if av > peak:
                peak = av
            acc += float(v * v)
        rms = int(math.sqrt(acc / n)) if n else 0
        return peak, rms

    if sample_width == 4:
        peak = 0
        acc = 0.0
        n = len(pcm_frames) // 4
        for (v,) in struct.iter_unpack("<i", pcm_frames[: n * 4]):
            av = abs(int(v))
            if av > peak:
                peak = av
            acc += float(v * v)
        rms = int(math.sqrt(acc / n)) if n else 0
        return peak, rms

    return 0, 0


def _normalize_clone_audio(filename: str, content: bytes) -> tuple[str, bytes, str, dict]:
    if shutil.which("ffmpeg") is None:
        return filename, content, "application/octet-stream", {"normalized": False, "normalization_reason": "ffmpeg_unavailable"}

    input_suffix = os.path.splitext(filename or "audio")[1] or ".bin"
    input_path = None
    output_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=input_suffix, delete=False) as src:
            src.write(content)
            input_path = src.name
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as dst:
            output_path = dst.name

        completed = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                input_path,
                "-ac",
                "1",
                "-ar",
                str(settings.VOICE_CLONE_TARGET_SAMPLE_RATE),
                output_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            logging.warning("ffmpeg normalization failed for %s: %s", filename, completed.stderr.decode("utf-8", "ignore"))
            return filename, content, "application/octet-stream", {"normalized": False, "normalization_reason": "ffmpeg_failed"}

        with open(output_path, "rb") as normalized:
            normalized_bytes = normalized.read()
        normalized_name = f"{os.path.splitext(filename or 'audio')[0]}.wav"
        return normalized_name, normalized_bytes, "audio/wav", {
            "normalized": True,
            "target_sample_rate": settings.VOICE_CLONE_TARGET_SAMPLE_RATE,
        }
    finally:
        for path in (input_path, output_path):
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass


def _prepare_clone_file(upload: UploadFile, content: bytes) -> tuple[tuple[str, bytes, str], dict]:
    filename = upload.filename or "audio"
    normalized_name, normalized_bytes, normalized_type, normalization_meta = _normalize_clone_audio(filename, content)
    content_type = normalized_type if normalization_meta.get("normalized") else (upload.content_type or "application/octet-stream")
    duration_seconds = _get_audio_duration_seconds(normalized_bytes) or _get_audio_duration_seconds(content)
    if duration_seconds is not None:
        if duration_seconds < settings.VOICE_CLONE_MIN_FILE_SECONDS:
            raise HTTPException(status_code=400, detail=f"Audio file '{filename}' is too short for voice cloning.")
        if duration_seconds > settings.VOICE_CLONE_MAX_FILE_SECONDS:
            raise HTTPException(status_code=400, detail=f"Audio file '{filename}' is too long for voice cloning.")

    diagnostics = {
        "filename": filename,
        "normalized_filename": normalized_name,
        "normalized": normalization_meta.get("normalized", False),
        "duration_seconds": round(duration_seconds, 2) if duration_seconds is not None else None,
        "content_type": content_type,
    }
    diagnostics.update(normalization_meta)

    if content_type == "audio/wav":
        wav_metrics = _analyze_wav_bytes(normalized_bytes)
        diagnostics.update(wav_metrics)
        rms_ratio = wav_metrics.get("rms_ratio")
        peak_ratio = wav_metrics.get("peak_ratio")
        if rms_ratio is not None and rms_ratio < 0.01:
            raise HTTPException(status_code=400, detail=f"Audio file '{filename}' is too quiet or silent for cloning.")
        if peak_ratio is not None and peak_ratio >= 0.99:
            diagnostics["possible_clipping"] = True

    return (normalized_name, normalized_bytes, content_type), diagnostics


@router.post("/clone")
async def clone_voice(
    user_id: int = Form(...),
    name: str = Form(...),
    files: list[UploadFile] = File(...),
    remove_background_noise: bool = Form(True),
    description: str | None = Form(None),
):
    """Create a voice clone from uploaded audio; returns ElevenLabs voice_id."""
    if not files:
        raise HTTPException(status_code=400, detail="At least one audio file is required")
    if len(files) > settings.VOICE_CLONE_MAX_FILES:
        raise HTTPException(status_code=400, detail=f"Upload up to {settings.VOICE_CLONE_MAX_FILES} audio files per clone.")

    file_tuples: list[tuple[str, bytes, str]] = []
    diagnostics: list[dict] = []
    total_duration = 0.0
    os.makedirs(VOICE_CLONE_UPLOADS_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    for i, f in enumerate(files):
        ct = f.content_type or "application/octet-stream"
        if not ct.startswith("audio/"):
            raise HTTPException(status_code=400, detail=f"Invalid file type: {f.filename or 'unknown'}. Use audio (MP3, WAV, etc.).")
        content = await f.read()
        if not content:
            raise HTTPException(status_code=400, detail=f"File is empty: {f.filename or 'unknown'}.")
        processed_file, processed_diagnostics = _prepare_clone_file(f, content)
        file_tuples.append(processed_file)
        diagnostics.append(processed_diagnostics)
        total_duration += processed_diagnostics.get("duration_seconds") or 0.0
        # Store copy in project folder for inspection
        saved_filename = processed_file[0]
        base = saved_filename.rsplit(".", 1)[0] if saved_filename.find(".") >= 0 else saved_filename
        ext = saved_filename.rsplit(".", 1)[-1].lower() if saved_filename.find(".") >= 0 else "bin"
        if ext in ("mp3", "wav", "m4a", "ogg", "webm", "flac"):
            pass
        else:
            ext = "bin"
        save_name = f"user{user_id}_{ts}_{i}_{base}.{ext}"
        save_path = os.path.join(VOICE_CLONE_UPLOADS_DIR, save_name)
        try:
            with open(save_path, "wb") as out:
                out.write(processed_file[1])
        except OSError as e:
            logging.warning("Could not save clone audio to %s: %s", save_path, e)

    if total_duration and total_duration < settings.VOICE_CLONE_MIN_TOTAL_SECONDS:
        raise HTTPException(status_code=400, detail="Please upload more clean speech audio before cloning this voice.")

    try:
        started_at = time.perf_counter()
        result = await add_voice(
            name=name,
            files=file_tuples,
            user_id=user_id,
            description=description,
            remove_background_noise=remove_background_noise,
        )
        clone_meta = {
            "status": "completed",
            "remove_background_noise": remove_background_noise,
            "description": description,
            "file_count": len(file_tuples),
            "total_duration_seconds": round(total_duration, 2),
            "latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
            "diagnostics": diagnostics,
            "voice_id": result.get("voice_id"),
        }
        try:
            safe_partial_update(
                table_name="Users",
                id_field="id",
                record_id=user_id,
                optional_payload={
                    "voice_clone_last_status": "completed",
                    "voice_clone_last_error": None,
                    "voice_clone_last_metadata": clone_meta,
                },
            )
        except Exception:
            logging.exception("Failed to persist clone diagnostics for user %s", user_id)
        result["diagnostics"] = diagnostics
        return result
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        try:
            safe_partial_update(
                table_name="Users",
                id_field="id",
                record_id=user_id,
                optional_payload={
                    "voice_clone_last_status": "failed",
                    "voice_clone_last_error": str(e),
                    "voice_clone_last_metadata": {
                        "remove_background_noise": remove_background_noise,
                        "description": description,
                        "file_count": len(file_tuples),
                        "total_duration_seconds": round(total_duration, 2),
                        "diagnostics": diagnostics,
                    },
                },
            )
        except Exception:
            logging.exception("Failed to persist clone failure diagnostics for user %s", user_id)
        _raise_http_from_httpx(e)


# Default sentence used to generate a voice preview from ElevenLabs
VOICE_PREVIEW_TEXT = "Hello, how are you?"


@router.get("/preview")
async def voice_preview(
    voice_id: str = Query(..., min_length=1, description="ElevenLabs voice_id"),
    speed: float = Query(1.0, ge=0.7, le=1.2),
):
    """Generate and return a short audio preview for the given ElevenLabs voice_id."""
    try:
        result = await text_to_speech(
            voice_id=voice_id,
            text=VOICE_PREVIEW_TEXT,
            model_id="eleven_multilingual_v2",
            output_format="mp3_44100_128",
            speed=speed,
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        _raise_http_from_httpx(e)
    return Response(content=result.audio_bytes, media_type=result.content_type)


class VoiceSettingsRequest(BaseModel):
    stability: float | None = Field(None, ge=0.0, le=1.0)
    similarity_boost: float | None = Field(None, ge=0.0, le=1.0)
    style: float | None = Field(None, ge=0.0, le=1.0)
    use_speaker_boost: bool | None = None
    speed: float | None = Field(None, ge=0.7, le=1.2)


class SpeakRequest(BaseModel):
    voice_id: str = Field(..., min_length=1)
    story_id: int = Field(..., description="Story id; story text is read from Stories.story")
    model_id: str = Field(default="eleven_multilingual_v2")
    output_format: str | None = Field(default=None, description="Optional ElevenLabs output format override")
    seed: int | None = Field(default=None, ge=0, le=4294967295)
    voice_settings: VoiceSettingsRequest | None = None

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
            output_format=request.output_format,
            seed=request.seed,
            voice_settings=request.voice_settings.model_dump(exclude_none=True) if request.voice_settings else None,
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
    return {"url": result["url"], "content_type": result["content_type"]}
    # return {"format_text": result["format_text"], "text_with_breaks": result["text_with_breaks"]}

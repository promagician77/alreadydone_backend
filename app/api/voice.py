import logging
import os
import shutil
import subprocess
import tempfile
import time
import json as _json
import wave
import math
import struct
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.db_utils import safe_partial_update
from app.core.debug_user import debug_log, user_id_from_story
from app.core.elevenlabs import add_voice, text_to_speech
from app.core.story_audio import generate_and_store_story_audio
from app.core.supabase_client import get_supabase

try:
    from mutagen import File as MutagenFile
except ImportError:
    MutagenFile = None

router = APIRouter(prefix="/voice", tags=["voice"])

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
            f.write(_json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


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

    def _ffmpeg_to_wav(audio_filter: str | None = None) -> tuple[str, bytes, str, dict]:
        command = [
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-ac",
            "1",
            "-ar",
            str(settings.VOICE_CLONE_TARGET_SAMPLE_RATE),
        ]
        if audio_filter:
            command.extend(["-af", audio_filter])
        command.append(output_path)

        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            logging.warning(
                "ffmpeg normalization failed for %s: %s",
                filename,
                completed.stderr.decode("utf-8", "ignore"),
            )
            return filename, content, "application/octet-stream", {
                "normalized": False,
                "normalization_reason": "ffmpeg_failed",
            }

        with open(output_path, "rb") as normalized:
            normalized_bytes = normalized.read()
        normalized_name = f"{os.path.splitext(filename or 'audio')[0]}.wav"
        return normalized_name, normalized_bytes, "audio/wav", {
            "normalized": True,
            "target_sample_rate": settings.VOICE_CLONE_TARGET_SAMPLE_RATE,
            "audio_filter": audio_filter,
        }

    input_suffix = os.path.splitext(filename or "audio")[1] or ".bin"
    input_path = None
    output_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=input_suffix, delete=False) as src:
            src.write(content)
            input_path = src.name
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as dst:
            output_path = dst.name

        normalized_name, normalized_bytes, normalized_type, normalization_meta = _ffmpeg_to_wav()
        if not normalization_meta.get("normalized"):
            return normalized_name, normalized_bytes, normalized_type, normalization_meta

        metrics = _analyze_wav_bytes(normalized_bytes)
        rms_ratio = metrics.get("rms_ratio")
        normalization_meta["pre_gain_rms_ratio"] = rms_ratio

        if (
            settings.VOICE_CLONE_AUTO_GAIN_QUIET_AUDIO
            and rms_ratio is not None
            and rms_ratio < settings.VOICE_CLONE_MIN_RMS_RATIO
        ):
            boosted_name, boosted_bytes, boosted_type, boosted_meta = _ffmpeg_to_wav(
                audio_filter="loudnorm=I=-20:TP=-2:LRA=7",
            )
            if boosted_meta.get("normalized"):
                boosted_metrics = _analyze_wav_bytes(boosted_bytes)
                boosted_rms_ratio = boosted_metrics.get("rms_ratio")
                normalization_meta["post_gain_rms_ratio"] = boosted_rms_ratio
                if boosted_rms_ratio is not None and boosted_rms_ratio > rms_ratio:
                    boosted_meta["auto_gain_applied"] = True
                    boosted_meta["pre_gain_rms_ratio"] = rms_ratio
                    boosted_meta["post_gain_rms_ratio"] = boosted_rms_ratio
                    if boosted_rms_ratio >= settings.VOICE_CLONE_MIN_RMS_RATIO:
                        return boosted_name, boosted_bytes, boosted_type, boosted_meta
                    # loudnorm helped but still under threshold — try linear gain (quiet speech / heavy NR in source).
                    vol_name, vol_bytes, vol_type, vol_meta = _ffmpeg_to_wav(
                        audio_filter="loudnorm=I=-20:TP=-2:LRA=7,volume=12dB",
                    )
                    if vol_meta.get("normalized"):
                        vol_metrics = _analyze_wav_bytes(vol_bytes)
                        vol_rms = vol_metrics.get("rms_ratio")
                        normalization_meta["post_loudnorm_volume_rms_ratio"] = vol_rms
                        if vol_rms is not None and vol_rms >= settings.VOICE_CLONE_MIN_RMS_RATIO:
                            vol_meta["auto_gain_applied"] = True
                            vol_meta["pre_gain_rms_ratio"] = rms_ratio
                            vol_meta["post_gain_rms_ratio"] = vol_rms
                            return vol_name, vol_bytes, vol_type, vol_meta
                        if vol_rms is not None and vol_rms > boosted_rms_ratio:
                            vol_meta["auto_gain_applied"] = True
                            vol_meta["pre_gain_rms_ratio"] = rms_ratio
                            vol_meta["post_gain_rms_ratio"] = vol_rms
                            return vol_name, vol_bytes, vol_type, vol_meta
                    return boosted_name, boosted_bytes, boosted_type, boosted_meta

            # loudnorm failed or did not normalize — fixed boost for borderline uploads
            vol_name, vol_bytes, vol_type, vol_meta = _ffmpeg_to_wav(audio_filter="volume=18dB")
            if vol_meta.get("normalized"):
                vol_metrics = _analyze_wav_bytes(vol_bytes)
                vol_rms = vol_metrics.get("rms_ratio")
                normalization_meta["post_volume_boost_rms_ratio"] = vol_rms
                if vol_rms is not None and vol_rms > rms_ratio:
                    vol_meta["auto_gain_applied"] = True
                    vol_meta["pre_gain_rms_ratio"] = rms_ratio
                    vol_meta["post_gain_rms_ratio"] = vol_rms
                    return vol_name, vol_bytes, vol_type, vol_meta

        return normalized_name, normalized_bytes, normalized_type, normalization_meta
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
        if rms_ratio is not None and rms_ratio < settings.VOICE_CLONE_MIN_RMS_RATIO:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Audio file '{filename}' is too quiet or silent for cloning. "
                    "Please record closer to the mic or speak louder."
                ),
            )
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
    supabase = get_supabase()
    debug_log(
        "voice.clone",
        "start",
        user_id=user_id,
        supabase=supabase,
        name=name,
        file_count=len(files),
        remove_background_noise=remove_background_noise,
    )
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
        debug_log(
            "voice.clone",
            "success",
            user_id=user_id,
            supabase=supabase,
            voice_id=result.get("voice_id"),
            diagnostics=diagnostics,
        )
        return result
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        debug_log("voice.clone", "error", user_id=user_id, supabase=supabase, error=str(e))
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
    force_regenerate: bool = Field(default=False, description="Ignore an existing playUrl and create fresh audio")

@router.get("/speak/{story_id}")
async def get_story_play_url(story_id: int):
    """Return the playUrl for the given story_id. 404 if story not found or playUrl not set."""
    supabase = get_supabase()
    uid = user_id_from_story(supabase, story_id)
    debug_log("voice.speak", "start", user_id=uid, supabase=supabase, story_id=story_id)
    r = supabase.table("Stories").select("playUrl").eq("id", story_id).or_("is_deleted.eq.false,is_deleted.is.null").execute()
    rows = list(r.data or [])
    if not rows:
        raise HTTPException(status_code=404, detail="Story not found")
    row = rows[0]
    play_url = (row.get("playUrl") or "").strip()
    if not play_url:
        debug_log("voice.speak", "no_play_url", user_id=uid, supabase=supabase, story_id=story_id)
        raise HTTPException(status_code=404, detail="Story has no play URL yet")
    debug_log("voice.speak", "success", user_id=uid, supabase=supabase, story_id=story_id, play_url=play_url)
    return {"playUrl": play_url}


@router.post("/generate_audio")
async def speak(request: SpeakRequest):
    started_at = time.perf_counter()
    supabase = get_supabase()
    uid = user_id_from_story(supabase, request.story_id)
    debug_log(
        "voice.generate_audio",
        "start",
        user_id=uid,
        supabase=supabase,
        story_id=request.story_id,
        voice_id=request.voice_id,
        force_regenerate=request.force_regenerate,
        model_id=request.model_id,
    )
    _agent_log(
        hypothesis_id="A",
        location="app/api/voice.py:generate_audio:entry",
        message="generate_audio request start",
        data={
            "storyId": request.story_id,
            "hasVoiceId": bool((request.voice_id or "").strip()),
            "modelId": request.model_id,
            "hasOutputFormat": request.output_format is not None,
            "hasSeed": request.seed is not None,
            "hasVoiceSettings": request.voice_settings is not None,
            "forceRegenerate": request.force_regenerate,
        },
    )

    if not request.force_regenerate:
        try:
            existing = (
                supabase.table("Stories")
                .select("playUrl")
                .eq("id", request.story_id)
                .or_("is_deleted.eq.false,is_deleted.is.null")
                .execute()
            )
            rows = list(existing.data or [])
            play_url = (rows[0].get("playUrl") or "").strip() if rows else ""
            if play_url:
                debug_log(
                    "voice.generate_audio",
                    "cached",
                    user_id=uid,
                    supabase=supabase,
                    story_id=request.story_id,
                    play_url=play_url,
                )
                _agent_log(
                    hypothesis_id="A",
                    location="app/api/voice.py:generate_audio:cached",
                    message="generate_audio returned existing playUrl",
                    data={
                        "storyId": request.story_id,
                        "elapsedMs": round((time.perf_counter() - started_at) * 1000, 2),
                    },
                )
                return {"url": play_url, "content_type": "audio/mpeg"}
        except Exception as e:
            _agent_log(
                hypothesis_id="A",
                location="app/api/voice.py:generate_audio:cached_check_failed",
                message="generate_audio cached playUrl check failed; continuing",
                data={
                    "storyId": request.story_id,
                    "elapsedMs": round((time.perf_counter() - started_at) * 1000, 2),
                    "errorType": type(e).__name__,
                },
            )

    if request.force_regenerate:
        try:
            safe_partial_update(
                table_name="Stories",
                id_field="id",
                record_id=request.story_id,
                optional_payload={
                    "playUrl": None,
                    "storage": None,
                    "audio_generation_status": "started",
                    "audio_generation_error": None,
                },
            )
        except Exception:
            logging.exception("Failed to clear cached audio fields for story %s", request.story_id)

    job_started = time.perf_counter()
    _agent_log(
        hypothesis_id="A",
        location="app/api/voice.py:generate_audio:sync_start",
        message="generate_audio synchronous generation start",
        data={"storyId": request.story_id},
    )
    try:
        result = await generate_and_store_story_audio(
            story_id=request.story_id,
            voice_id=request.voice_id,
            model_id=request.model_id,
            output_format=request.output_format,
            seed=request.seed,
            voice_settings=request.voice_settings.model_dump(exclude_none=True) if request.voice_settings else None,
            apply_postprocess=True,
        )
    except Exception as e:
        debug_log(
            "voice.generate_audio",
            "error",
            user_id=uid,
            supabase=supabase,
            story_id=request.story_id,
            error=str(e),
        )
        error_detail = str(e)
        if isinstance(e, httpx.HTTPStatusError):
            try:
                error_detail = f"HTTP {e.response.status_code}: {e.response.text}"
            except Exception:
                error_detail = str(e)
        if error_detail and len(error_detail) > 1500:
            error_detail = error_detail[:1500]
        _agent_log(
            hypothesis_id="A",
            location="app/api/voice.py:generate_audio:sync_error",
            message="generate_audio synchronous generation error",
            data={
                "storyId": request.story_id,
                "elapsedMs": round((time.perf_counter() - job_started) * 1000, 2),
                "errorType": type(e).__name__,
                "errorDetail": error_detail,
            },
        )
        if isinstance(e, httpx.HTTPStatusError):
            _raise_http_from_httpx(e)
        raise HTTPException(status_code=502, detail=f"Audio generation failed: {error_detail}")

    play_url = ((result or {}).get("url") or "").strip() if result else ""
    if not play_url:
        _agent_log(
            hypothesis_id="A",
            location="app/api/voice.py:generate_audio:sync_empty",
            message="generate_audio produced no play URL",
            data={
                "storyId": request.story_id,
                "elapsedMs": round((time.perf_counter() - job_started) * 1000, 2),
            },
        )
        raise HTTPException(status_code=502, detail="Audio generation produced no play URL")

    content_type = (result or {}).get("content_type") or "audio/mpeg"
    response_body: dict = {"url": play_url, "content_type": content_type}
    play_length = (result or {}).get("play_length")
    if play_length is not None:
        response_body["play_length"] = play_length

    debug_log(
        "voice.generate_audio",
        "success",
        user_id=uid,
        supabase=supabase,
        story_id=request.story_id,
        play_url=play_url,
        play_length=play_length,
    )
    _agent_log(
        hypothesis_id="A",
        location="app/api/voice.py:generate_audio:sync_success",
        message="generate_audio returned new playUrl",
        data={
            "storyId": request.story_id,
            "elapsedMs": round((time.perf_counter() - started_at) * 1000, 2),
        },
    )
    return response_body

"""ElevenLabs API client for voice cloning."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.core.supabase_client import get_supabase

ELEVENLABS_ADD_VOICE_URL = "/v1/voices/add"


async def add_voice(
    *,
    name: str,
    user_id: int = None,
    files: list[tuple[str, bytes, str]],
    description: str | None = None,
    remove_background_noise: bool = False,
) -> dict:
    headers = {
        "xi-api-key": settings.ELEVENLABS_API_KEY,
        "Accept": "application/json",
    }
    data: dict = {
        "name": name,
        "remove_background_noise": str(remove_background_noise).lower(),
    }
    if description is not None:
        data["description"] = description

    # Build multipart: form fields + file(s)
    files_payload: list[tuple[str, tuple[str, bytes, str]]] = [
        ("files", (filename, content, content_type))
        for filename, content, content_type in files
    ]

    async with httpx.AsyncClient(
        base_url=settings.ELEVENLABS_BASE_URL,
        timeout=settings.ELEVENLABS_TTS_TIMEOUT_SECONDS,
    ) as client:
        response = await client.post(
            ELEVENLABS_ADD_VOICE_URL,
            headers=headers,
            data=data,
            files=files_payload,
        )
        response.raise_for_status()
        result = response.json()
        generated_voice_id = result.get("voice_id")
        if user_id is not None and generated_voice_id:
            supabase = get_supabase()
            supabase.table("Users").update({"voice_id": generated_voice_id}).eq("id", user_id).execute()
        return result


def _tts_url(voice_id: str) -> str:
    return f"/v1/text-to-speech/{voice_id}"


@dataclass
class TTSResult:
    audio_bytes: bytes
    content_type: str
    output_format: str
    request_id: str | None
    history_item_id: str | None


def default_voice_settings() -> dict:
    return {
        "stability": settings.ELEVENLABS_TTS_STABILITY,
        "similarity_boost": settings.ELEVENLABS_TTS_SIMILARITY_BOOST,
        "style": settings.ELEVENLABS_TTS_STYLE,
        "speed": settings.ELEVENLABS_TTS_SPEED,
        "use_speaker_boost": settings.ELEVENLABS_TTS_USE_SPEAKER_BOOST,
    }


async def text_to_speech(
    *,
    voice_id: str,
    text: str,
    model_id: str = "eleven_multilingual_v2",
    output_format: str | None = None,
    voice_settings: dict | None = None,
    enable_ssml: bool = False,
    previous_text: str | None = None,
    next_text: str | None = None,
    previous_request_ids: list[str] | None = None,
    next_request_ids: list[str] | None = None,
    speed: float | None = None,
    seed: int | None = None,
) -> TTSResult:
    headers = {
        "xi-api-key": settings.ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/octet-stream",
    }
    request_voice_settings = default_voice_settings()
    if voice_settings:
        request_voice_settings.update({k: v for k, v in voice_settings.items() if v is not None})
    if speed is not None:
        request_voice_settings["speed"] = speed

    payload: dict = {
        "text": text,
        "model_id": model_id,
        "voice_settings": request_voice_settings,
    }
    if enable_ssml:
        payload["enable_ssml"] = True
    if previous_text:
        payload["previous_text"] = previous_text
    if next_text:
        payload["next_text"] = next_text
    if previous_request_ids:
        payload["previous_request_ids"] = previous_request_ids[:3]
    if next_request_ids:
        payload["next_request_ids"] = next_request_ids[:3]
    if seed is not None:
        payload["seed"] = seed

    params = {"output_format": output_format or settings.ELEVENLABS_TTS_OUTPUT_FORMAT}
    retries = max(0, settings.ELEVENLABS_TTS_MAX_RETRIES)
    last_exc: Exception | None = None
    async with httpx.AsyncClient(
        base_url=settings.ELEVENLABS_BASE_URL,
        timeout=settings.ELEVENLABS_TTS_TIMEOUT_SECONDS,
    ) as client:
        for attempt in range(retries + 1):
            try:
                response = await client.post(
                    _tts_url(voice_id),
                    headers=headers,
                    json=payload,
                    params=params,
                )
                response.raise_for_status()
                content_type = response.headers.get("content-type", "application/octet-stream")
                request_id = (
                    response.headers.get("request-id")
                    or response.headers.get("x-request-id")
                    or response.headers.get("xi-request-id")
                )
                history_item_id = response.headers.get("history-item-id") or response.headers.get("xi-history-item-id")
                return TTSResult(
                    audio_bytes=response.content,
                    content_type=content_type,
                    output_format=params["output_format"],
                    request_id=request_id,
                    history_item_id=history_item_id,
                )
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                last_exc = exc
                should_retry = attempt < retries and not (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response is not None
                    and exc.response.status_code < 500
                )
                if not should_retry:
                    raise
                await asyncio.sleep(0.5 * (attempt + 1))

    assert last_exc is not None
    raise last_exc

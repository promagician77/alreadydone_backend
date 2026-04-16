"""ElevenLabs API client for voice cloning."""

from __future__ import annotations

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

    async with httpx.AsyncClient(base_url=settings.ELEVENLABS_BASE_URL, timeout=60.0) as client:
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


async def text_to_speech(
    *,
    voice_id: str,
    text: str,
    model_id: str = "eleven_multilingual_v2",
    enable_ssml: bool = False,
    previous_text: str | None = None,
    next_text: str | None = None,
) -> tuple[bytes, str]:
    headers = {
        "xi-api-key": settings.ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload: dict = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            # "style": 0.15,
            "use_speaker_boost": True,
        },
    }
    if enable_ssml:
        payload["enable_ssml"] = True
    if previous_text:
        payload["previous_text"] = previous_text
    if next_text:
        payload["next_text"] = next_text

    async with httpx.AsyncClient(base_url=settings.ELEVENLABS_BASE_URL, timeout=60.0) as client:
        response = await client.post(
            _tts_url(voice_id),
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "audio/mpeg")
        return (response.content, content_type)

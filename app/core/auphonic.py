"""Auphonic client for post-processing generated story audio."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.core.config import settings

_AUPHONIC_SIMPLE_PRODUCTIONS_URL = "/api/simple/productions.json"
_AUDIO_FORMAT_PREFERENCE = ("mp3", "aac", "wav", "flac", "alac", "opus", "vorbis")
_FAILED_STATUS_TERMS = ("error", "failed", "failure", "cancel", "canceled", "stopped")


class AuphonicError(RuntimeError):
    """Raised when Auphonic processing fails."""


class AuphonicTimeoutError(AuphonicError):
    """Raised when Auphonic processing does not complete in time."""


@dataclass
class AuphonicResult:
    audio_bytes: bytes
    content_type: str
    file_ext: str
    output_format: str
    filename: str
    metadata: dict


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"bearer {settings.AUPHONIC_API_KEY}",
        "Accept": "application/json",
    }


def _extract_data(payload: dict) -> dict:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise AuphonicError("Auphonic response did not include a data object.")
    return data


def _looks_like_uuid(value: str) -> bool:
    text = (value or "").strip()
    return len(text) == 22 and text.isalnum()


def _response_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
        parts: list[str] = []
        error_message = payload.get("error_message")
        if error_message:
            parts.append(str(error_message).strip())
        form_errors = payload.get("form_errors")
        if isinstance(form_errors, dict) and form_errors:
            parts.append(json.dumps(form_errors, sort_keys=True))
        if parts:
            return " | ".join(p for p in parts if p)
    except Exception:
        pass

    body = response.text.strip()
    return body if body else f"HTTP {response.status_code}"


async def _resolve_preset_identifier(client: httpx.AsyncClient, preset: str) -> str:
    preset = (preset or "").strip()
    if not preset:
        raise AuphonicError("AUPHONIC_PRESET is empty.")
    if _looks_like_uuid(preset):
        return preset

    response = await client.get(
        "/api/presets.json",
        headers=_auth_headers(),
        params={"minimal_data": 1},
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data")
    if not isinstance(data, list):
        raise AuphonicError("Could not load presets from Auphonic.")

    exact_match = None
    casefold_match = None
    for item in data:
        if not isinstance(item, dict):
            continue
        preset_name = (item.get("preset_name") or "").strip()
        preset_uuid = (item.get("uuid") or "").strip()
        if not preset_name or not preset_uuid:
            continue
        if preset_name == preset:
            exact_match = preset_uuid
            break
        if casefold_match is None and preset_name.casefold() == preset.casefold():
            casefold_match = preset_uuid

    resolved = exact_match or casefold_match
    if resolved:
        return resolved

    available = [
        (item.get("preset_name") or "").strip()
        for item in data
        if isinstance(item, dict) and (item.get("preset_name") or "").strip()
    ]
    raise AuphonicError(
        f"Auphonic preset '{preset}' was not found for this account. "
        f"Available presets: {', '.join(available) if available else 'none'}"
    )


def _is_done_status(status: int | None, status_string: str | None) -> bool:
    if status == 3:
        return True
    return (status_string or "").strip().lower() == "done"


def _is_failed_status(status: int | None, status_string: str | None) -> bool:
    if status is not None and status >= 4:
        return True
    text = (status_string or "").strip().lower()
    return any(term in text for term in _FAILED_STATUS_TERMS)


def _content_type_for_format(output_format: str, filename: str | None = None) -> str:
    fmt = (output_format or "").strip().lower()
    if fmt == "wav":
        return "audio/wav"
    if fmt in ("mp3", "mp3-vbr"):
        return "audio/mpeg"
    if fmt == "aac":
        return "audio/mp4"
    if fmt == "alac":
        return "audio/mp4"
    if fmt == "flac":
        return "audio/flac"
    if fmt == "opus":
        return "audio/opus"
    if fmt == "vorbis":
        return "audio/ogg"
    if filename:
        lower = filename.lower()
        if lower.endswith(".wav"):
            return "audio/wav"
        if lower.endswith(".mp3"):
            return "audio/mpeg"
        if lower.endswith(".m4a") or lower.endswith(".mp4"):
            return "audio/mp4"
        if lower.endswith(".flac"):
            return "audio/flac"
        if lower.endswith(".opus"):
            return "audio/opus"
        if lower.endswith(".ogg"):
            return "audio/ogg"
    return "application/octet-stream"


def _file_extension_for_output(output_format: str, filename: str | None = None) -> str:
    fmt = (output_format or "").strip().lower()
    if fmt == "mp3-vbr":
        return "mp3"
    if fmt in _AUDIO_FORMAT_PREFERENCE:
        return "m4a" if fmt in ("aac", "alac") else fmt
    if filename and "." in filename:
        return filename.rsplit(".", 1)[-1].lower()
    return "bin"


def _download_url_with_bearer_token(download_url: str) -> str:
    parts = urlsplit(download_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if "bearer_token" not in query:
        query["bearer_token"] = settings.AUPHONIC_API_KEY
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _pick_output_file(production: dict) -> dict:
    output_files = production.get("output_files")
    if not isinstance(output_files, list) or not output_files:
        raise AuphonicError("Auphonic production returned no output files.")

    candidates: list[tuple[int, dict]] = []
    for item in output_files:
        if not isinstance(item, dict):
            continue
        download_url = item.get("download_url")
        output_format = (item.get("format") or "").strip().lower()
        if not download_url or output_format not in _AUDIO_FORMAT_PREFERENCE:
            continue
        try:
            priority = _AUDIO_FORMAT_PREFERENCE.index(output_format)
        except ValueError:
            priority = len(_AUDIO_FORMAT_PREFERENCE)
        candidates.append((priority, item))

    if not candidates:
        raise AuphonicError("Auphonic production completed without a downloadable audio result.")

    candidates.sort(key=lambda entry: entry[0])
    return candidates[0][1]


async def process_audio(
    *,
    audio_bytes: bytes,
    filename: str,
    title: str,
    input_content_type: str | None = None,
    output_basename: str | None = None,
) -> AuphonicResult:
    if not settings.AUPHONIC_API_KEY:
        raise AuphonicError("Auphonic is enabled but AUPHONIC_API_KEY is not configured.")
    if not settings.AUPHONIC_PRESET:
        raise AuphonicError("Auphonic is enabled but AUPHONIC_PRESET is not configured.")

    started_at = time.perf_counter()
    request_data = {
        "preset": settings.AUPHONIC_PRESET,
        "title": title,
        "action": "start",
    }
    if output_basename:
        request_data["output_basename"] = output_basename

    try:
        async with httpx.AsyncClient(
            base_url=settings.AUPHONIC_BASE_URL,
            timeout=settings.AUPHONIC_TIMEOUT_SECONDS,
            follow_redirects=True,
        ) as client:
            preset_identifier = await _resolve_preset_identifier(client, settings.AUPHONIC_PRESET)
            create_response = await client.post(
                _AUPHONIC_SIMPLE_PRODUCTIONS_URL,
                headers=_auth_headers(),
                data={**request_data, "preset": preset_identifier},
                files={
                    "input_file": (
                        filename,
                        audio_bytes,
                        input_content_type or _content_type_for_format("", filename),
                    )
                },
            )
            try:
                create_response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = _response_error_detail(create_response)
                raise AuphonicError(
                    f"Auphonic create production failed ({create_response.status_code}): {detail}"
                ) from exc
            production = _extract_data(create_response.json())
            production_uuid = (production.get("uuid") or "").strip()
            if not production_uuid:
                raise AuphonicError("Auphonic did not return a production UUID.")

            deadline = time.perf_counter() + settings.AUPHONIC_MAX_WAIT_SECONDS
            while True:
                details_response = await client.get(
                    f"/api/production/{production_uuid}.json",
                    headers=_auth_headers(),
                )
                details_response.raise_for_status()
                production = _extract_data(details_response.json())

                status = production.get("status")
                status_string = production.get("status_string")
                if _is_done_status(status, status_string):
                    break
                if _is_failed_status(status, status_string):
                    raise AuphonicError(
                        f"Auphonic production {production_uuid} failed with status "
                        f"{status!r} ({status_string!r})."
                    )
                if time.perf_counter() >= deadline:
                    raise AuphonicTimeoutError(
                        f"Auphonic production {production_uuid} did not finish within "
                        f"{settings.AUPHONIC_MAX_WAIT_SECONDS:.0f} seconds."
                    )
                await asyncio.sleep(settings.AUPHONIC_POLL_INTERVAL_SECONDS)

            output_file = _pick_output_file(production)
            download_url = _download_url_with_bearer_token(str(output_file["download_url"]))
            download_response = await client.get(download_url)
            download_response.raise_for_status()

            output_format = (output_file.get("format") or "").strip().lower()
            filename = (output_file.get("filename") or filename or "processed-audio").strip()
            content_type = download_response.headers.get(
                "content-type",
                _content_type_for_format(output_format, filename),
            )
            file_ext = _file_extension_for_output(output_format, filename)

            metadata = {
                "provider": "auphonic",
                "preset": settings.AUPHONIC_PRESET,
                "production_uuid": production_uuid,
                "status": production.get("status"),
                "status_string": production.get("status_string"),
                "output_file": {
                    "format": output_file.get("format"),
                    "filename": output_file.get("filename"),
                    "download_url": output_file.get("download_url"),
                    "size": output_file.get("size"),
                    "bitrate": output_file.get("bitrate"),
                },
                "processing_ms": round((time.perf_counter() - started_at) * 1000, 2),
            }

            return AuphonicResult(
                audio_bytes=download_response.content,
                content_type=content_type,
                file_ext=file_ext,
                output_format=output_format,
                filename=filename,
                metadata=metadata,
            )
    except httpx.HTTPError as exc:
        raise AuphonicError(f"Auphonic API request failed: {exc}") from exc

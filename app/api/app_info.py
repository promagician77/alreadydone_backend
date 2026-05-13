"""Public mobile app metadata (update nudges)."""

from fastapi import APIRouter

from app.core.config import settings
from app.core.mobile_app_release import resolve_mobile_latest

router = APIRouter(tags=["mobile"])


@router.get("/mobile-app/update")
def mobile_app_update():
    resolved = resolve_mobile_latest(settings)
    if resolved is None:
        return {"enabled": False}

    message = (settings.MOBILE_UPDATE_MESSAGE or "").strip()
    return {
        "enabled": True,
        "latest_build": resolved.latest_build,
        "latest_version": resolved.latest_version,
        "latest_version_plus": resolved.latest_version_plus,
        "message": message or None,
        "ios_store_url": settings.MOBILE_IOS_STORE_URL.strip() or None,
        "android_store_url": settings.MOBILE_ANDROID_PLAY_STORE_URL.strip() or None,
    }

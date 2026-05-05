"""Public mobile app metadata (update nudges)."""

from fastapi import APIRouter

from app.core.config import settings

router = APIRouter(tags=["mobile"])


@router.get("/mobile-app/update")
def mobile_app_update():
    if settings.MOBILE_LATEST_BUILD_NUMBER <= 0:
        return {"enabled": False}

    message = (settings.MOBILE_UPDATE_MESSAGE or "").strip()
    return {
        "enabled": True,
        "latest_build": settings.MOBILE_LATEST_BUILD_NUMBER,
        "latest_version": settings.MOBILE_LATEST_VERSION.strip() or None,
        "message": message or None,
        "ios_store_url": settings.MOBILE_IOS_STORE_URL.strip() or None,
        "android_store_url": settings.MOBILE_ANDROID_PLAY_STORE_URL.strip() or None,
    }

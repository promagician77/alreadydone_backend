"""FCM (Firebase Cloud Messaging) for push notifications. Requires Firebase Admin SDK credentials."""

import logging
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

_fcm_initialized = False

DAILY_NOTIFICATION_CATEGORY = "DAILY_STORY"
DAILY_STORY_ROUTE = "/onboarding/desire"


def _ensure_fcm():
    global _fcm_initialized
    if _fcm_initialized:
        return True
    path = (settings.FIREBASE_CREDENTIALS_PATH or "").strip()
    if not path:
        logger.warning("[fcm] init skipped: FIREBASE_CREDENTIALS_PATH is not set in .env")
        return False
    cred_path = Path(path)
    if not cred_path.is_file():
        logger.warning(
            "[fcm] init skipped: credentials file not found at %s (resolved=%s)",
            path,
            cred_path.resolve(),
        )
        return False
    try:
        import firebase_admin
        from firebase_admin import credentials

        cred = credentials.Certificate(path)
        firebase_admin.initialize_app(cred)
        _fcm_initialized = True
        return True
    except Exception as e:
        logger.warning("[fcm] init failed: %s", e)
        return False


def send_push(
    token: str,
    title: str,
    body: str,
    reminder_type: str | None = None,
) -> bool:
    """Send a push notification to one FCM token. Returns True if sent successfully."""
    if not token or not token.strip():
        logger.warning("[fcm] send skipped: no token provided (type=%s)", reminder_type)
        return False
    if not _ensure_fcm():
        logger.warning("[fcm] send skipped: not initialized (type=%s)", reminder_type)
        return False
    token_preview = f"{token.strip()[:12]}..."
    try:
        from firebase_admin import messaging

        data: dict[str, str] = {}
        if reminder_type:
            data["type"] = reminder_type
        apns_config = None
        android_config = None

        if reminder_type == "daily":
            data["route"] = DAILY_STORY_ROUTE
            logger.info(
                "[fcm] daily push token=%s route=%s category=%s",
                token_preview,
                DAILY_STORY_ROUTE,
                DAILY_NOTIFICATION_CATEGORY,
            )
            apns_config = messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        category=DAILY_NOTIFICATION_CATEGORY,
                        sound="default",
                    ),
                ),
            )
            android_config = messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    click_action="FLUTTER_NOTIFICATION_CLICK",
                    channel_id="fcm_default_channel",
                ),
            )

        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data=data,
            token=token.strip(),
            apns=apns_config,
            android=android_config,
        )
        messaging.send(message)
        logger.info("[fcm] send ok type=%s token=%s title=%r", reminder_type or "default", token_preview, title)
        return True
    except Exception as e:
        logger.warning("[fcm] send failed type=%s token=%s: %s", reminder_type, token_preview, e)
        return False

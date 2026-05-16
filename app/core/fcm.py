"""FCM (Firebase Cloud Messaging) for push notifications. Requires Firebase Admin SDK credentials."""

import logging
from pathlib import Path

from app.core.config import settings

_fcm_initialized = False

DAILY_NOTIFICATION_CATEGORY = "DAILY_STORY"
DAILY_STORY_ROUTE = "/onboarding/desire"


def _ensure_fcm():
    global _fcm_initialized
    if _fcm_initialized:
        return True
    path = (settings.FIREBASE_CREDENTIALS_PATH or "").strip()
    if not path or not Path(path).is_file():
        return False
    try:
        import firebase_admin
        from firebase_admin import credentials

        cred = credentials.Certificate(path)
        firebase_admin.initialize_app(cred)
        _fcm_initialized = True
        return True
    except Exception as e:
        logging.warning("FCM init failed: %s", e)
        return False


def send_push(
    token: str,
    title: str,
    body: str,
    reminder_type: str | None = None,
) -> bool:
    """Send a push notification to one FCM token. Returns True if sent successfully."""
    if not token or not token.strip():
        logging.warning("FCM send skipped: no token provided")
        return False
    if not _ensure_fcm():
        logging.warning("FCM send skipped: not initialized")
        return False
    try:
        from firebase_admin import messaging

        data: dict[str, str] = {}
        if reminder_type:
            data["type"] = reminder_type
        apns_config = None
        android_config = None

        if reminder_type == "daily":
            data["route"] = DAILY_STORY_ROUTE
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
        logging.info("FCM send successful (type=%s)", reminder_type or "default")
        return True
    except Exception as e:
        logging.warning("FCM send failed for token %s...: %s", token[:20] if token else "", e)
        return False

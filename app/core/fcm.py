"""FCM (Firebase Cloud Messaging) for push notifications. Requires Firebase Admin SDK credentials."""

import logging
from pathlib import Path

from app.core.config import settings

_fcm_initialized = False


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


def send_push(token: str, title: str, body: str) -> bool:
    print("Sending push notification to token %s", token)
    """Send a push notification to one FCM token. Returns True if sent successfully."""
    if not token or not token.strip():
        print("FCM send failed for token %s...: %s", token[:20] if token else "", "No token provided")
        return False
    if not _ensure_fcm():
        print("FCM send failed for token %s...: %s", token[:20] if token else "", "FCM not initialized")
        return False
    try:
        from firebase_admin import messaging

        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            token=token.strip(),
        )
        messaging.send(message)
        print("FCM send successful for token %s", token)
        return True
    except Exception as e:
        print("FCM send failed for token %s...: %s", token[:20] if token else "", e)
        logging.warning("FCM send failed for token %s...: %s", token[:20] if token else "", e)
        return False

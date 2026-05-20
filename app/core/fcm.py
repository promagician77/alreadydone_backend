"""FCM (Firebase Cloud Messaging) for push notifications. Requires Firebase Admin SDK credentials."""

import logging
import os
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

_fcm_initialized = False
_fcm_init_failure_logged = False

MONDAY_NOTIFICATION_CATEGORY = "MONDAY_STORY"
Thursday_NOTIFICATION_CATEGORY = "Thursday_STORY"
STORY_REMINDER_ROUTE = "/onboarding/desire"

_STORY_REMINDER_APNS_CATEGORIES = {
    "monday": MONDAY_NOTIFICATION_CATEGORY,
    "thursday": Thursday_NOTIFICATION_CATEGORY,
}


def _credentials_diagnostics(cred_path: Path) -> str:
    """Build a one-line diagnostic string for missing/invalid credential paths."""
    parent = cred_path.parent
    parts = [
        f"cwd={Path.cwd()}",
        f"parent={parent}",
        f"parent_exists={parent.is_dir()}",
    ]
    if parent.is_dir():
        try:
            names = sorted(p.name for p in parent.iterdir() if p.is_file())[:25]
            parts.append(f"files_in_parent={names!r}")
        except OSError as exc:
            parts.append(f"parent_list_error={exc}")
    return "; ".join(parts)


def _log_init_failure(message: str, *args) -> None:
    """Log FCM init failure once at ERROR; later attempts at DEBUG to avoid log spam."""
    global _fcm_init_failure_logged
    if not _fcm_init_failure_logged:
        logger.error(message, *args)
        _fcm_init_failure_logged = True
    else:
        logger.debug(message, *args)


def is_fcm_ready() -> bool:
    """Return True if Firebase Admin SDK is initialized and ready to send."""
    return _fcm_initialized


def probe_fcm_at_startup() -> bool:
    """
    Validate FCM credentials when the reminder scheduler starts.
    Logs actionable diagnostics if the service account file is missing or invalid.
    """
    path = (settings.FIREBASE_CREDENTIALS_PATH or "").strip()
    logger.info(
        "[fcm] startup probe: FIREBASE_CREDENTIALS_PATH=%r",
        path or "(not set)",
    )
    ready = _ensure_fcm()
    if ready:
        logger.info("[fcm] startup probe: ready to send push notifications")
    else:
        logger.error(
            "[fcm] startup probe: NOT ready — scheduled reminders will not be delivered "
            "until FIREBASE_CREDENTIALS_PATH points to a readable Firebase service account JSON"
        )
    return ready


def _ensure_fcm():
    global _fcm_initialized
    if _fcm_initialized:
        return True
    path = (settings.FIREBASE_CREDENTIALS_PATH or "").strip()
    if not path:
        _log_init_failure(
            "[fcm] init failed: FIREBASE_CREDENTIALS_PATH is not set. "
            "Set it in .env to the Firebase Admin SDK JSON file path."
        )
        return False
    cred_path = Path(path)
    if not cred_path.is_file():
        _log_init_failure(
            "[fcm] init failed: credentials file not found at %s (resolved=%s). "
            "Mount or copy the service account JSON into the container. Diagnostics: %s",
            path,
            cred_path.resolve(),
            _credentials_diagnostics(cred_path),
        )
        return False
    if not os.access(path, os.R_OK):
        _log_init_failure(
            "[fcm] init failed: credentials file exists but is not readable: %s",
            cred_path.resolve(),
        )
        return False
    try:
        import firebase_admin
        from firebase_admin import credentials

        cred = credentials.Certificate(path)
        firebase_admin.initialize_app(cred)
        _fcm_initialized = True
        logger.info("[fcm] initialized successfully from %s", cred_path.resolve())
        return True
    except Exception as e:
        _log_init_failure("[fcm] init failed: %s (path=%s)", e, cred_path.resolve())
        return False


def send_push(
    token: str,
    title: str,
    body: str,
    reminder_type: str | None = None,
    user_id: int | None = None,
) -> bool:
    user_label = f"user_id={user_id}" if user_id is not None else "user_id=?"
    if not token or not token.strip():
        if reminder_type in _STORY_REMINDER_APNS_CATEGORIES:
            print(f"[fcm/{reminder_type}] send skipped: no token ({user_label})", flush=True)
        logger.warning(
            "[fcm] send skipped: no token provided (type=%s, %s)",
            reminder_type,
            user_label,
        )
        return False
    if not _ensure_fcm():
        if reminder_type in _STORY_REMINDER_APNS_CATEGORIES:
            print(
                f"[fcm/{reminder_type}] send skipped: FCM not initialized ({user_label})",
                flush=True,
            )
        if _fcm_init_failure_logged:
            logger.debug(
                "[fcm] send skipped: not initialized (type=%s, %s)",
                reminder_type,
                user_label,
            )
        else:
            logger.warning(
                "[fcm] send skipped: not initialized (type=%s, %s)",
                reminder_type,
                user_label,
            )
        return False
    token_preview = f"{token.strip()[:12]}..."
    try:
        from firebase_admin import messaging

        data: dict[str, str] = {}
        if reminder_type:
            data["type"] = reminder_type
        apns_config = None
        android_config = None

        if reminder_type in _STORY_REMINDER_APNS_CATEGORIES:
            apns_category = _STORY_REMINDER_APNS_CATEGORIES[reminder_type]
            data["route"] = STORY_REMINDER_ROUTE
            data["title"] = title
            data["body"] = body
            print(
                f"[fcm/{reminder_type}] preparing push {user_label} token={token_preview} "
                f"route={STORY_REMINDER_ROUTE} apns_category={apns_category} data={data}",
                flush=True,
            )
            apns_config = messaging.APNSConfig(
                headers={
                    "apns-push-type": "alert",
                    "apns-priority": "10",
                },
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        alert=messaging.ApsAlert(title=title, body=body),
                        category=apns_category,
                        sound="default",
                    ),
                ),
            )
            print(
                f"[fcm/{reminder_type}] apns aps.category={apns_category!r} "
                f"(platform-specific alert, no top-level notification field)",
                flush=True,
            )
            android_config = messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    title=title,
                    body=body,
                    click_action="FLUTTER_NOTIFICATION_CLICK",
                    channel_id="fcm_default_channel",
                ),
            )
            message = messaging.Message(
                data=data,
                token=token.strip(),
                apns=apns_config,
                android=android_config,
            )
        else:
            message = messaging.Message(
                notification=messaging.Notification(title=title, body=body),
                data=data,
                token=token.strip(),
                apns=apns_config,
                android=android_config,
            )
        
        message_id = messaging.send(message)
        if reminder_type in _STORY_REMINDER_APNS_CATEGORIES:
            print(
                f"[fcm/{reminder_type}] send ok {user_label} token={token_preview} "
                f"message_id={message_id} title={title!r} apns_config_set=True",
                flush=True,
            )
        else:
            logger.info(
                "[fcm] send ok type=%s %s token=%s title=%r",
                reminder_type or "default",
                user_label,
                token_preview,
                title,
            )
        return True
    except Exception as e:
        if reminder_type in _STORY_REMINDER_APNS_CATEGORIES:
            print(
                f"[fcm/{reminder_type}] send FAILED {user_label} token={token_preview} error={e}",
                flush=True,
            )
        logger.warning(
            "[fcm] send failed type=%s %s token=%s: %s",
            reminder_type,
            user_label,
            token_preview,
            e,
        )
        return False

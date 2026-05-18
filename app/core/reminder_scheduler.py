import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import settings
from app.core.fcm import probe_fcm_at_startup, send_push
from app.core.supabase_client import get_supabase

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

# Notification copy
MORNING_TITLE = "Good Morning 🌞"
MORNING_BODY = "Start your day with a fresh story made just for you."
BEDTIME_TITLE = "Wind Down with a Story 🌙"
BEDTIME_BODY = "Your bedtime story is ready to help you relax."

DAILY_HOUR = 14
DAILY_MINUTE = 50
DAILY_TITLE = "Are You Ready for the New Best Day Ever?"
DAILY_BODY = "It's time to create your new daily manifestation story!"


def _parse_hour_minute(value) -> tuple[int, int] | None:
    if value is None:
        return None
    try:
        if isinstance(value, str):
            s = value.strip()
            if " " in s:
                s = s.split(" ")[-1]
            elif "T" in s:
                s = s.split("T")[-1]
            parts = s.replace(":", " ").split()
            if len(parts) >= 2:
                return int(parts[0]), int(parts[1])
            return None
        if hasattr(value, "hour") and hasattr(value, "minute"):
            return value.hour, value.minute
        return None
    except (ValueError, IndexError):
        return None


def _get_user_now(utc_now: datetime, user_timezone: str | None) -> tuple[int, int]:
    tz_str = (user_timezone or "").strip() or "UTC"
    try:
        tz = ZoneInfo(tz_str)
        local = utc_now.astimezone(tz)
        return local.hour, local.minute
    except Exception:
        return utc_now.hour, utc_now.minute

def _is_daily_reminder_time(hour: int, minute: int, user_id: int) -> bool:
    if user_id == 237:
        print(f"User {user_id} is daily reminder time: {hour}, {minute}")
    return (hour, minute) == (DAILY_HOUR, DAILY_MINUTE)


def _check_and_send_reminders():
    if not settings.FIREBASE_CREDENTIALS_PATH or not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        logger.warning(
            "[reminders] tick skipped: missing config "
            "(FIREBASE_CREDENTIALS_PATH=%s, SUPABASE_URL=%s, SUPABASE_KEY=%s)",
            bool(settings.FIREBASE_CREDENTIALS_PATH),
            bool(settings.SUPABASE_URL),
            bool(settings.SUPABASE_KEY),
        )
        return
    now_utc = datetime.now(timezone.utc)
    supabase = get_supabase()
    try:
        r = supabase.table("Users").select(
            "id", "fcm_token", "morningTime_Reminder", "bedTime_Reminder",
            "is_MorningTime_Reminder", "is_BedTime_Reminder", "timezone",
        ).not_.is_("fcm_token", "null").execute()

    except Exception as e:
        logger.warning("[reminders] query failed: %s", e)
        return

    rows = list(r.data or [])
    for row in rows:
        user_id = row.get("id")
        token = (row.get("fcm_token") or "").strip()
        if not token:
            continue
        timezone_name = row.get("timezone")
        current_hour, current_minute = _get_user_now(now_utc, timezone_name)

        morning_on = row.get("is_MorningTime_Reminder") in (True, "true")
        bedtime_on = row.get("is_BedTime_Reminder") in (True, "true")
        morning_hm = _parse_hour_minute(row.get("morningTime_Reminder"))
        bedtime_hm = _parse_hour_minute(row.get("bedTime_Reminder"))

        if morning_on and morning_hm and morning_hm == (current_hour, current_minute):
            logger.info(
                "[reminders] sending morning user_id=%s local_time=%02d:%02d tz=%r",
                user_id,
                current_hour,
                current_minute,
                timezone_name,
            )
            if send_push(
                token, MORNING_TITLE, MORNING_BODY, reminder_type="morning", user_id=user_id
            ):
                logger.info("[reminders] sent morning user_id=%s", user_id)
            else:
                logger.warning("[reminders] morning send failed user_id=%s", user_id)
        if bedtime_on and bedtime_hm and bedtime_hm == (current_hour, current_minute):
            logger.info(
                "[reminders] sending bedtime user_id=%s local_time=%02d:%02d tz=%r",
                user_id,
                current_hour,
                current_minute,
                timezone_name,
            )
            if send_push(
                token, BEDTIME_TITLE, BEDTIME_BODY, reminder_type="bedtime", user_id=user_id
            ):
                logger.info("[reminders] sent bedtime user_id=%s", user_id)
            else:
                logger.warning("[reminders] bedtime send failed user_id=%s", user_id)
        if _is_daily_reminder_time(current_hour, current_minute):
            logger.info(
                "[reminders] sending daily user_id=%s local_time=%02d:%02d tz=%r "
                "(target=%02d:%02d)",
                user_id,
                current_hour,
                current_minute,
                timezone_name,
                DAILY_HOUR,
                DAILY_MINUTE,
            )
            if send_push(
                token, DAILY_TITLE, DAILY_BODY, reminder_type="daily", user_id=user_id
            ):
                logger.info("[reminders] sent daily user_id=%s", user_id)
            else:
                logger.warning(
                    "[reminders] daily send failed user_id=%s — check [fcm] logs above "
                    "(credentials file must exist at FIREBASE_CREDENTIALS_PATH)",
                    user_id,
                )


def start_reminder_scheduler():
    if not scheduler.running:
        probe_fcm_at_startup()
        scheduler.add_job(_check_and_send_reminders, "cron", minute="*", id="reminders")
        scheduler.start()
        logger.info(
            "[reminders] scheduler started (every minute, daily at %02d:%02d local)",
            DAILY_HOUR,
            DAILY_MINUTE,
        )


def stop_reminder_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("[reminders] scheduler stopped")


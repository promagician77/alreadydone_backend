import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import settings
from app.core.fcm import send_push
from app.core.supabase_client import get_supabase

scheduler = AsyncIOScheduler()

# Notification copy
MORNING_TITLE = "Good Morning 🌞"
MORNING_BODY = "Start your day with a fresh story made just for you."
BEDTIME_TITLE = "Wind Down with a Story 🌙"
BEDTIME_BODY = "Your bedtime story is ready to help you relax."

DAILY_HOUR = 14
DAILY_MINUTE = 20
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

def _is_daily_reminder_time(hour: int, minute: int) -> bool:
    return (hour, minute) == (DAILY_HOUR, DAILY_MINUTE)


def _check_and_send_reminders():
    if not settings.FIREBASE_CREDENTIALS_PATH or not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        return
    now_utc = datetime.now(timezone.utc)
    print(f"now_utc: {now_utc}")

    supabase = get_supabase()
    try:
        r = supabase.table("Users").select(
            "id", "fcm_token", "morningTime_Reminder", "bedTime_Reminder",
            "is_MorningTime_Reminder", "is_BedTime_Reminder", "timezone",
        ).not_.is_("fcm_token", "null").execute()

        print(f"r: {r}")
    except Exception as e:
        logging.warning("Reminder query failed: %s", e)
        return

    rows = list(r.data or [])
    for row in rows:
        token = (row.get("fcm_token") or "").strip()
        if not token:
            continue
        current_hour, current_minute = _get_user_now(now_utc, row.get("timezone"))
        morning_on = row.get("is_MorningTime_Reminder") in (True, "true")
        bedtime_on = row.get("is_BedTime_Reminder") in (True, "true")
        morning_hm = _parse_hour_minute(row.get("morningTime_Reminder"))
        bedtime_hm = _parse_hour_minute(row.get("bedTime_Reminder"))

        if morning_on and morning_hm and morning_hm == (current_hour, current_minute):
            if send_push(token, MORNING_TITLE, MORNING_BODY, reminder_type="morning"):
                logging.info("Sent morning reminder to user %s", row.get("id"))
        if bedtime_on and bedtime_hm and bedtime_hm == (current_hour, current_minute):
            if send_push(token, BEDTIME_TITLE, BEDTIME_BODY, reminder_type="bedtime"):
                logging.info("Sent bedtime reminder to user %s", row.get("id"))
        if _is_daily_reminder_time(current_hour, current_minute):
            if send_push(token, DAILY_TITLE, DAILY_BODY, reminder_type="daily"):
                logging.info("Sent daily reminder to user %s", row.get("id"))


def start_reminder_scheduler():
    if not scheduler.running:
        scheduler.add_job(_check_and_send_reminders, "cron", minute="*", id="reminders")
        scheduler.start()
        logging.info("Reminder scheduler started (every minute)")


def stop_reminder_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logging.info("Reminder scheduler stopped")


import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import settings
from app.core.fcm import probe_fcm_at_startup, send_push
from app.core.supabase_client import get_supabase

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

MORNING_TITLE = "Good Morning 🌞"
MORNING_BODY = "Start your day with a fresh story made just for you."
BEDTIME_TITLE = "Wind Down with a Story 🌙"
BEDTIME_BODY = "Your bedtime story is ready to help you relax."

MONDAY_HOUR = 15
MONDAY_MINUTE = 50
MONDAY_TITLE = "Create Your Story Now"
MONDAY_BODY = "Then hear it in your voice all day."

FRIDAY_HOUR = 15
FRIDAY_MINUTE = 50
FRIDAY_TITLE = "Your Stories Are Waiting"
FRIDAY_BODY = "Tap to hear them in your voice."

_STORY_REMINDER_TEST_USER_ID = 237
_TEST_OVERRIDE_WEEKDAY = 2  # Wednesday (Mon=0)
_TEST_MONDAY_HOUR = 15
_TEST_MONDAY_MINUTE = 55
_TEST_FRIDAY_HOUR = 16
_TEST_FRIDAY_MINUTE = 0


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


def _get_user_now(utc_now: datetime, user_timezone: str | None) -> tuple[int, int, int]:
    tz_str = (user_timezone or "").strip() or "UTC"
    try:
        tz = ZoneInfo(tz_str)
        local = utc_now.astimezone(tz)
        return local.hour, local.minute, local.weekday()
    except Exception:
        return utc_now.hour, utc_now.minute, utc_now.weekday()


def _is_monday_reminder_time(hour: int, minute: int, weekday: int, user_id: int) -> bool:
    matches = weekday == 0 and (hour, minute) == (MONDAY_HOUR, MONDAY_MINUTE)
    if user_id == _STORY_REMINDER_TEST_USER_ID:
        matches = matches or (
            weekday == _TEST_OVERRIDE_WEEKDAY
            and (hour, minute) == (_TEST_MONDAY_HOUR, _TEST_MONDAY_MINUTE)
        )
    if user_id == 237:
        print(
            f"[reminders/monday] time check user_id={user_id} "
            f"local={hour:02d}:{minute:02d} weekday={weekday} "
            f"target=Mon {MONDAY_HOUR:02d}:{MONDAY_MINUTE:02d} "
            f"or Wed {_TEST_MONDAY_HOUR:02d}:{_TEST_MONDAY_MINUTE:02d} "
            f"(test user only) matches={matches}",
            flush=True,
        )
    return matches


def _is_friday_reminder_time(hour: int, minute: int, weekday: int, user_id: int) -> bool:
    matches = weekday == 4 and (hour, minute) == (FRIDAY_HOUR, FRIDAY_MINUTE)
    if user_id == _STORY_REMINDER_TEST_USER_ID:
        matches = matches or (
            weekday == _TEST_OVERRIDE_WEEKDAY
            and (hour, minute) == (_TEST_FRIDAY_HOUR, _TEST_FRIDAY_MINUTE)
        )
    if user_id == _STORY_REMINDER_TEST_USER_ID:
        print(
            f"[reminders/friday] time check user_id={user_id} "
            f"local={hour:02d}:{minute:02d} weekday={weekday} "
            f"target=Fri {FRIDAY_HOUR:02d}:{FRIDAY_MINUTE:02d} "
            f"or Wed {_TEST_FRIDAY_HOUR:02d}:{_TEST_FRIDAY_MINUTE:02d} "
            f"(test user only) matches={matches}",
            flush=True,
        )
    return matches


def _maybe_send_story_reminder(
    *,
    token: str,
    user_id: int,
    due: bool,
    reminder_type: str,
    title: str,
    body: str,
    apns_category: str,
    local_hour: int,
    local_minute: int,
    timezone_name: str | None,
) -> None:
    if not due:
        return
    if user_id == _STORY_REMINDER_TEST_USER_ID:
        print(
            f"[reminders/{reminder_type}] sending user_id={user_id} "
            f"local={local_hour:02d}:{local_minute:02d} tz={timezone_name!r} "
            f"title={title!r} apns_category={apns_category}",
            flush=True,
        )
        if send_push(token, title, body, reminder_type=reminder_type, user_id=user_id):
            print(
                f"[reminders/{reminder_type}] send_push returned ok user_id={user_id}",
                flush=True,
            )
        else:
            print(
                f"[reminders/{reminder_type}] send_push FAILED user_id={user_id} — "
                f"see [fcm/{reminder_type}] prints above",
                flush=True,
            )
    else:
        print(
            f"[reminders/{reminder_type}] due at {local_hour:02d}:{local_minute:02d} "
            f"but skipped (test gate) user_id={user_id}",
            flush=True,
        )


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
        current_hour, current_minute, current_weekday = _get_user_now(
            now_utc, timezone_name
        )

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
        _maybe_send_story_reminder(
            token=token,
            user_id=user_id,
            due=_is_monday_reminder_time(
                current_hour, current_minute, current_weekday, user_id
            ),
            reminder_type="monday",
            title=MONDAY_TITLE,
            body=MONDAY_BODY,
            apns_category="MONDAY_STORY",
            local_hour=current_hour,
            local_minute=current_minute,
            timezone_name=timezone_name,
        )
        _maybe_send_story_reminder(
            token=token,
            user_id=user_id,
            due=_is_friday_reminder_time(
                current_hour, current_minute, current_weekday, user_id
            ),
            reminder_type="friday",
            title=FRIDAY_TITLE,
            body=FRIDAY_BODY,
            apns_category="FRIDAY_STORY",
            local_hour=current_hour,
            local_minute=current_minute,
            timezone_name=timezone_name,
        )


def start_reminder_scheduler():
    if not scheduler.running:
        probe_fcm_at_startup()
        scheduler.add_job(_check_and_send_reminders, "cron", minute="*", id="reminders")
        scheduler.start()
        logger.info(
            "[reminders] scheduler started (every minute, Mon/Fri story reminders at %02d:%02d local)",
            MONDAY_HOUR,
            MONDAY_MINUTE,
        )


def stop_reminder_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("[reminders] scheduler stopped")


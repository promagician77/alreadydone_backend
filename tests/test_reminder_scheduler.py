import pathlib
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Allow importing reminder_scheduler without full backend deps in test env.
_mock_apscheduler = MagicMock()
_mock_apscheduler.schedulers.asyncio.AsyncIOScheduler = MagicMock
sys.modules.setdefault("apscheduler", _mock_apscheduler)
sys.modules.setdefault("apscheduler.schedulers", _mock_apscheduler.schedulers)
sys.modules.setdefault(
    "apscheduler.schedulers.asyncio", _mock_apscheduler.schedulers.asyncio
)
sys.modules.setdefault("pydantic_settings", MagicMock())

_config_mod = MagicMock()
_config_mod.settings = MagicMock(
    FIREBASE_CREDENTIALS_PATH="",
    SUPABASE_URL="",
    SUPABASE_KEY="",
)
sys.modules.setdefault("app.core.config", _config_mod)
sys.modules.setdefault("app.core.fcm", MagicMock())
sys.modules.setdefault("app.core.supabase_client", MagicMock())

from app.core.reminder_scheduler import (
    DAILY_HOUR,
    DAILY_MINUTE,
    _get_user_now,
    _is_daily_reminder_time,
    _parse_hour_minute,
    _check_and_send_reminders,
)


class ParseHourMinuteTests(unittest.TestCase):
    def test_hh_mm_ss_string(self):
        self.assertEqual(_parse_hour_minute("08:30:00"), (8, 30))

    def test_iso_datetime_string(self):
        self.assertEqual(_parse_hour_minute("2024-01-15T21:45:00"), (21, 45))

    def test_datetime_object(self):
        dt = datetime(2024, 1, 15, 7, 15, 0)
        self.assertEqual(_parse_hour_minute(dt), (7, 15))

    def test_invalid_returns_none(self):
        self.assertIsNone(_parse_hour_minute(None))
        self.assertIsNone(_parse_hour_minute("invalid"))


class GetUserNowTests(unittest.TestCase):
    def test_america_new_york_8am(self):
        # 2024-06-15 12:00 UTC = 08:00 EDT (America/New_York)
        utc = datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(_get_user_now(utc, "America/New_York"), (8, 0))

    def test_invalid_timezone_falls_back_to_utc(self):
        utc = datetime(2024, 6, 15, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(_get_user_now(utc, "Not/A/Timezone"), (8, 0))


class DailyReminderTimeTests(unittest.TestCase):
    def test_exactly_8_am(self):
        self.assertTrue(_is_daily_reminder_time(DAILY_HOUR, DAILY_MINUTE))

    def test_not_daily_times(self):
        self.assertFalse(_is_daily_reminder_time(8, 1))
        self.assertFalse(_is_daily_reminder_time(7, 59))
        self.assertFalse(_is_daily_reminder_time(9, 0))


class CheckAndSendRemindersTests(unittest.TestCase):
    @patch("app.core.reminder_scheduler.send_push")
    @patch("app.core.reminder_scheduler.get_supabase")
    @patch("app.core.reminder_scheduler.settings")
    def test_sends_daily_at_8am_local(
        self, mock_settings, mock_get_supabase, mock_send_push
    ):
        mock_settings.FIREBASE_CREDENTIALS_PATH = "/fake/path"
        mock_settings.SUPABASE_URL = "https://example.supabase.co"
        mock_settings.SUPABASE_KEY = "key"

        mock_send_push.return_value = True
        mock_table = MagicMock()
        mock_get_supabase.return_value.table.return_value = mock_table
        mock_table.select.return_value.not_.is_.return_value.execute.return_value = MagicMock(
            data=[
                {
                    "id": 1,
                    "fcm_token": "token-abc",
                    "morningTime_Reminder": None,
                    "bedTime_Reminder": None,
                    "is_MorningTime_Reminder": False,
                    "is_BedTime_Reminder": False,
                    "timezone": "UTC",
                }
            ]
        )

        fixed_utc = datetime(2024, 6, 15, DAILY_HOUR, DAILY_MINUTE, tzinfo=timezone.utc)
        with patch(
            "app.core.reminder_scheduler.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = fixed_utc
            mock_datetime.side_effect = lambda *a, **k: datetime(*a, **k)

            _check_and_send_reminders()

        daily_calls = [
            c for c in mock_send_push.call_args_list if c.kwargs.get("reminder_type") == "daily"
        ]
        self.assertEqual(len(daily_calls), 1)
        self.assertIn("Best Day Ever", daily_calls[0].args[1])

    @patch("app.core.reminder_scheduler.send_push")
    @patch("app.core.reminder_scheduler.get_supabase")
    @patch("app.core.reminder_scheduler.settings")
    def test_skips_daily_when_not_8am(
        self, mock_settings, mock_get_supabase, mock_send_push
    ):
        mock_settings.FIREBASE_CREDENTIALS_PATH = "/fake/path"
        mock_settings.SUPABASE_URL = "https://example.supabase.co"
        mock_settings.SUPABASE_KEY = "key"

        mock_table = MagicMock()
        mock_get_supabase.return_value.table.return_value = mock_table
        mock_table.select.return_value.not_.is_.return_value.execute.return_value = MagicMock(
            data=[
                {
                    "id": 1,
                    "fcm_token": "token-abc",
                    "morningTime_Reminder": None,
                    "bedTime_Reminder": None,
                    "is_MorningTime_Reminder": False,
                    "is_BedTime_Reminder": False,
                    "timezone": "UTC",
                }
            ]
        )

        fixed_utc = datetime(2024, 6, 15, 9, 0, tzinfo=timezone.utc)
        with patch(
            "app.core.reminder_scheduler.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = fixed_utc
            mock_datetime.side_effect = lambda *a, **k: datetime(*a, **k)

            _check_and_send_reminders()

        daily_calls = [
            c for c in mock_send_push.call_args_list if c.kwargs.get("reminder_type") == "daily"
        ]
        self.assertEqual(len(daily_calls), 0)

    @patch("app.core.reminder_scheduler.send_push")
    @patch("app.core.reminder_scheduler.get_supabase")
    @patch("app.core.reminder_scheduler.settings")
    def test_morning_only_when_enabled_and_time_matches(
        self, mock_settings, mock_get_supabase, mock_send_push
    ):
        mock_settings.FIREBASE_CREDENTIALS_PATH = "/fake/path"
        mock_settings.SUPABASE_URL = "https://example.supabase.co"
        mock_settings.SUPABASE_KEY = "key"

        mock_send_push.return_value = True
        mock_table = MagicMock()
        mock_get_supabase.return_value.table.return_value = mock_table
        mock_table.select.return_value.not_.is_.return_value.execute.return_value = MagicMock(
            data=[
                {
                    "id": 2,
                    "fcm_token": "token-xyz",
                    "morningTime_Reminder": "07:30:00",
                    "bedTime_Reminder": None,
                    "is_MorningTime_Reminder": True,
                    "is_BedTime_Reminder": False,
                    "timezone": "UTC",
                }
            ]
        )

        fixed_utc = datetime(2024, 6, 15, 7, 30, tzinfo=timezone.utc)
        with patch(
            "app.core.reminder_scheduler.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = fixed_utc
            mock_datetime.side_effect = lambda *a, **k: datetime(*a, **k)

            _check_and_send_reminders()

        types = [c.kwargs.get("reminder_type") for c in mock_send_push.call_args_list]
        self.assertIn("morning", types)
        self.assertNotIn("daily", types)


if __name__ == "__main__":
    unittest.main()

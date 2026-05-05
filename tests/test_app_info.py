import pathlib
import sys
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import app


class MobileAppUpdateTests(unittest.TestCase):
    @patch("app.api.app_info.settings")
    def test_update_disabled_when_build_not_set(self, mock_settings):
        mock_settings.MOBILE_LATEST_BUILD_NUMBER = 0
        client = TestClient(app)
        r = client.get("/api/mobile-app/update")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body.get("enabled"))

    @patch("app.api.app_info.settings")
    def test_update_enabled_with_config(self, mock_settings):
        mock_settings.MOBILE_LATEST_BUILD_NUMBER = 42
        mock_settings.MOBILE_LATEST_VERSION = "1.2.0"
        mock_settings.MOBILE_UPDATE_MESSAGE = "Please update."
        mock_settings.MOBILE_IOS_STORE_URL = "https://apps.apple.com/app/example"
        mock_settings.MOBILE_ANDROID_PLAY_STORE_URL = "https://play.google.com/store/apps/details?id=x"

        client = TestClient(app)
        r = client.get("/api/mobile-app/update")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["enabled"])
        self.assertEqual(body["latest_build"], 42)
        self.assertEqual(body["latest_version"], "1.2.0")
        self.assertEqual(body["message"], "Please update.")
        self.assertEqual(body["ios_store_url"], "https://apps.apple.com/app/example")
        self.assertEqual(body["android_store_url"], "https://play.google.com/store/apps/details?id=x")


if __name__ == "__main__":
    unittest.main()

import pathlib
import sys
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.mobile_app_release import resolve_mobile_latest


class ResolveMobileLatestTests(unittest.TestCase):
    def test_plus_suffix_overrides_build_env(self):
        s = SimpleNamespace(
            MOBILE_LATEST_VERSION="1.0.8+2",
            MOBILE_LATEST_BUILD_NUMBER=99,
        )
        r = resolve_mobile_latest(s)
        assert r is not None
        self.assertEqual(r.latest_version, "1.0.8")
        self.assertEqual(r.latest_build, 2)
        self.assertEqual(r.latest_version_plus, "1.0.8+2")

    def test_version_and_separate_build(self):
        s = SimpleNamespace(
            MOBILE_LATEST_VERSION="1.0.8",
            MOBILE_LATEST_BUILD_NUMBER=2,
        )
        r = resolve_mobile_latest(s)
        assert r is not None
        self.assertEqual(r.latest_version, "1.0.8")
        self.assertEqual(r.latest_build, 2)
        self.assertEqual(r.latest_version_plus, "1.0.8+2")

    def test_disabled_when_empty(self):
        s = SimpleNamespace(
            MOBILE_LATEST_VERSION="",
            MOBILE_LATEST_BUILD_NUMBER=0,
        )
        self.assertIsNone(resolve_mobile_latest(s))


if __name__ == "__main__":
    unittest.main()

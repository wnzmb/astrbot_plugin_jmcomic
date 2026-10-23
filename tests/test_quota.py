import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_jmcomic.services.quota import FileQuotaService  # noqa: E402


class QuotaTest(unittest.TestCase):
    def test_record_and_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(
                send_state_file=Path(tmp) / "send_limits.json",
                daily_file_limit=2,
                hourly_file_limit=1,
            )
            service = FileQuotaService(settings)
            self.assertEqual(service.file_quota()["day_count"], 0)
            service.record_file_send()
            service.record_file_send()
            quota = service.file_quota()
            self.assertEqual(quota["day_count"], 2)
            self.assertEqual(quota["hour_count"], 2)
            self.assertTrue(service.quota_reached(quota))
            self.assertEqual(service.limit_text(0), "不限")
            self.assertEqual(service.quota_display(1, 0), "1/不限")


if __name__ == "__main__":
    unittest.main()

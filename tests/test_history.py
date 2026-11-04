import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_jmcomic.services.history import HistoryService  # noqa: E402


class HistoryServiceTest(unittest.TestCase):
    def _service(self, tmp, history_max):
        settings = SimpleNamespace(
            history_file=Path(tmp) / "history.json",
            history_max=history_max,
        )
        return HistoryService(settings)

    def test_add_user_keeps_newest_and_respects_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp, 2)

            for index in range(3):
                service.add_user("u1", {"id": index})

            items = service.get_user("u1")
            self.assertEqual([item["id"] for item in items], [2, 1])

    def test_history_max_zero_keeps_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp, 0)

            for index in range(3):
                service.add_user("u1", {"id": index})

            self.assertEqual(len(service.get_user("u1")), 3)

    def test_async_wrappers_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp, 10)

            count = asyncio.run(service.add_user_async("u2", {"id": 9}))

            self.assertEqual(count, 1)
            self.assertEqual(asyncio.run(service.get_user_async("u2"))[0]["id"], 9)
            data = asyncio.run(service.load_async())
            self.assertIn("u2", data)


if __name__ == "__main__":
    unittest.main()

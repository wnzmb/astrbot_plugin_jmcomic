import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_jmcomic.services.cache import CacheService  # noqa: E402
from astrbot_plugin_jmcomic.services.download import DownloadService  # noqa: E402


class NoDetailClient:
    def get_client(self):
        raise AssertionError("page_count 已知时不应再请求详情")


class DownloadCompleteTest(unittest.TestCase):
    def test_page_count_avoids_detail_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for index in range(1, 6):
                (folder / f"{index:05}.jpg").write_bytes(b"x")
            settings = SimpleNamespace(download_base_dir=folder)
            service = DownloadService(
                settings, NoDetailClient(), CacheService(settings), None
            )
            photo = SimpleNamespace(photo_id="1", page_arr=None, page_count=5)
            self.assertTrue(asyncio.run(service.is_photo_complete(photo, folder)))

    def test_incomplete_when_page_count_larger(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for index in range(1, 6):
                (folder / f"{index:05}.jpg").write_bytes(b"x")
            settings = SimpleNamespace(download_base_dir=folder)
            service = DownloadService(
                settings, NoDetailClient(), CacheService(settings), None
            )
            photo = SimpleNamespace(photo_id="1", page_arr=None, page_count=6)
            self.assertFalse(asyncio.run(service.is_photo_complete(photo, folder)))


if __name__ == "__main__":
    unittest.main()

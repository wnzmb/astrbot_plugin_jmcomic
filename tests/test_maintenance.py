import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_jmcomic.services.maintenance import MaintenanceService  # noqa: E402


class MaintenanceTest(unittest.TestCase):
    def test_clean_keeps_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = SimpleNamespace(
                download_base_dir=root / "downloads",
                zip_base_dir=root / "zips",
                data_dir=root,
            )
            chapter = settings.download_base_dir / "JM1" / "P1"
            chapter.mkdir(parents=True)
            old = time.time() - 8 * 86400
            old_images = [chapter / "00001.jpg", chapter / "00002.png"]
            for path in old_images:
                path.write_bytes(b"x")
                os.utime(path, (old, old))
            new_image = chapter / "00003.jpg"
            new_image.write_bytes(b"y")
            metadata = chapter / "metadata.json"
            metadata.write_text(json.dumps({"page_count": 3}), encoding="utf-8")

            service = MaintenanceService(settings)
            preview = service.scan_clean(7)
            self.assertEqual(preview["files"], 2)
            result = service.clean(7)
            self.assertEqual(result["files"], 2)
            self.assertTrue(metadata.exists())
            self.assertTrue(new_image.exists())
            self.assertFalse(old_images[0].exists())
            self.assertFalse(old_images[1].exists())

    def test_active_album_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = SimpleNamespace(
                download_base_dir=root / "downloads",
                zip_base_dir=root / "zips",
                data_dir=root,
            )
            chapter = settings.download_base_dir / "JM1" / "P1"
            chapter.mkdir(parents=True)
            image = chapter / "00001.jpg"
            image.write_bytes(b"x")
            os.utime(image, (time.time() - 8 * 86400,) * 2)
            service = MaintenanceService(settings)
            preview = service.scan_clean(7, {"1"})
            self.assertEqual(preview["files"], 0)
            self.assertEqual(preview["skipped_active"], ["JM1"])

class MaintenanceHandlerActiveTest(unittest.TestCase):
    def test_album_dirs_history_is_not_active(self):
        """BUG-031：历史下载目录不能算活动任务，否则 /jmclean 永远跳过。"""
        from astrbot_plugin_jmcomic.core.downloader import ProgressDownloader
        from astrbot_plugin_jmcomic.core.state import PluginState
        from astrbot_plugin_jmcomic.handlers.maintenance import MaintenanceHandler

        state = PluginState()
        state.active_downloads["u1"] = {"album_id": "43164"}
        ProgressDownloader._album_dirs["1228973"] = "C:/tmp/JM1228973"
        try:
            handler = MaintenanceHandler(None, None, None, state, None)
            active = handler._active_album_ids()
        finally:
            ProgressDownloader._album_dirs.pop("1228973", None)

        self.assertIn("43164", active)
        self.assertNotIn("1228973", active)

    def test_no_active_downloads_returns_empty(self):
        from astrbot_plugin_jmcomic.core.downloader import ProgressDownloader
        from astrbot_plugin_jmcomic.core.state import PluginState
        from astrbot_plugin_jmcomic.handlers.maintenance import MaintenanceHandler

        state = PluginState()
        ProgressDownloader._album_dirs["999"] = "C:/tmp/JM999"
        try:
            handler = MaintenanceHandler(None, None, None, state, None)
            self.assertEqual(handler._active_album_ids(), set())
        finally:
            ProgressDownloader._album_dirs.pop("999", None)

if __name__ == "__main__":
    unittest.main()

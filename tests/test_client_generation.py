import sys
import unittest
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_jmcomic.core.client import JmClientManager  # noqa: E402


class FakeSettings:
    def __init__(self):
        self.option = object()
        self.refresh_calls = 0
        self.login_ttl_minutes = 10
        self.username = ""
        self.password = ""

    def refresh_option(self):
        self.refresh_calls += 1
        self.option = object()
        return self.option


def _new_package(version: str):
    pkg = ModuleType("jmcomic")
    pkg.__version__ = version
    return pkg


class ClientGenerationTest(unittest.TestCase):
    """BUG-027 回归：jmcomic 模块被 AstrBot 重载后必须重建客户端。"""

    def setUp(self):
        self._saved = {
            key: value
            for key, value in sys.modules.items()
            if key == "jmcomic" or key.startswith("jmcomic.")
        }
        sys.modules["jmcomic"] = _new_package("gen1")
        self.settings = FakeSettings()
        self.manager = JmClientManager(self.settings)

    def tearDown(self):
        for key in [
            key for key in sys.modules if key == "jmcomic" or key.startswith("jmcomic.")
        ]:
            del sys.modules[key]
        sys.modules.update(self._saved)

    def test_no_rebuild_without_swap(self):
        cached = object()
        self.manager._cached_client = cached
        self.manager._sync_generation()
        self.assertIs(self.manager._cached_client, cached)
        self.assertEqual(self.settings.refresh_calls, 0)

    def test_rebuild_after_swap(self):
        self.manager._cached_client = object()
        sys.modules["jmcomic"] = _new_package("gen2")

        self.manager._sync_generation()

        self.assertIsNone(self.manager._cached_client)
        self.assertEqual(self.settings.refresh_calls, 1)

    def test_rebuild_only_once_per_generation(self):
        sys.modules["jmcomic"] = _new_package("gen2")
        self.manager._sync_generation()
        self.manager._sync_generation()
        self.assertEqual(self.settings.refresh_calls, 1)

    def test_handle_exception_recognizes_current_generation(self):
        exc = ModuleType("jmcomic.jm_exception")

        class MissingAlbumPhotoException(Exception):
            pass

        MissingAlbumPhotoException.__module__ = "jmcomic.jm_exception"
        exc.MissingAlbumPhotoException = MissingAlbumPhotoException
        sys.modules["jmcomic.jm_exception"] = exc

        message = self.manager.handle_exception(
            MissingAlbumPhotoException("404"), "查询"
        )
        self.assertIn("查无此本", message)


if __name__ == "__main__":
    unittest.main()

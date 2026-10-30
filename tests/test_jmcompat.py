import sys
import unittest
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_jmcomic.core import jmcompat  # noqa: E402


def _new_package(version: str):
    """构造一份“某一代”的假 jmcomic 包。"""
    pkg = ModuleType("jmcomic")
    pkg.__version__ = version
    exc = ModuleType("jmcomic.jm_exception")

    missing = type("MissingAlbumPhotoException", (Exception,), {})
    missing.__module__ = "jmcomic.jm_exception"
    exc.MissingAlbumPhotoException = missing

    retry = type("RequestRetryAllFailException", (Exception,), {})
    retry.__module__ = "jmcomic.jm_exception"
    exc.RequestRetryAllFailException = retry

    pkg.jm_exception = exc
    pkg.DownloadControl = type("DownloadControl", (), {})
    return pkg, exc


class JmCompatTest(unittest.TestCase):
    def setUp(self):
        self._saved = {
            key: value
            for key, value in sys.modules.items()
            if key == "jmcomic" or key.startswith("jmcomic.")
        }
        self.pkg, self.exc = _new_package("2.7.10-gen1")
        sys.modules["jmcomic"] = self.pkg
        sys.modules["jmcomic.jm_exception"] = self.exc

    def tearDown(self):
        for key in [
            key for key in sys.modules if key == "jmcomic" or key.startswith("jmcomic.")
        ]:
            del sys.modules[key]
        sys.modules.update(self._saved)

    def _swap_generation(self, version="2.7.10-gen2"):
        pkg, exc = _new_package(version)
        sys.modules["jmcomic"] = pkg
        sys.modules["jmcomic.jm_exception"] = exc
        return pkg, exc

    def test_generation_changes_after_module_swap(self):
        first = jmcompat.generation()
        self._swap_generation()
        self.assertNotEqual(first, jmcompat.generation())

    def test_attr_follows_current_generation(self):
        self.assertEqual(jmcompat.attr("__version__"), "2.7.10-gen1")
        self._swap_generation()
        self.assertEqual(jmcompat.attr("__version__"), "2.7.10-gen2")
        self.assertIsNone(jmcompat.attr("NotExists"))

    def test_exceptions_returns_current_classes(self):
        classes = jmcompat.exceptions(
            "MissingAlbumPhotoException", "RequestRetryAllFailException"
        )
        self.assertIn(self.exc.MissingAlbumPhotoException, classes)
        self.assertIn(self.exc.RequestRetryAllFailException, classes)

    def test_exceptions_ignores_unknown_names(self):
        classes = jmcompat.exceptions("MissingAlbumPhotoException", "NotExists")
        self.assertEqual(len(classes), 1)

    def test_is_exception_matches_current_generation(self):
        old_error = self.exc.MissingAlbumPhotoException("x")
        self.assertTrue(
            jmcompat.is_exception(old_error, "MissingAlbumPhotoException")
        )
        self._swap_generation()
        # 换代后旧异常不再被识别，避免跨代 isinstance 误判
        self.assertFalse(
            jmcompat.is_exception(old_error, "MissingAlbumPhotoException")
        )

    def test_is_exception_tolerates_plain_error(self):
        self.assertFalse(jmcompat.is_exception(ValueError("x"), "MissingAlbumPhotoException"))


if __name__ == "__main__":
    unittest.main()

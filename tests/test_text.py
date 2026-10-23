import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_jmcomic.services.text import TextService  # noqa: E402


class TextTest(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            sensitive_filter=True,
            custom_sensitive_map={"巨乳": "juru", "痴汉": "chihan"},
            convert_enabled=False,
            convert_title=False,
            convert_tags=True,
        )
        self.service = TextService(self.settings)

    def test_sanitize_and_restore(self):
        try:
            import pypinyin  # noqa: F401
        except ImportError:
            self.skipTest("pypinyin not installed")
        self.assertEqual(self.service.sanitize_tags(["巨乳", "痴汉"]), ["juru", "chihan"])
        self.assertEqual(self.service.restore_tag("juru"), "巨乳")
        self.assertEqual(self.service.restore_tag("chihan"), "痴汉")
        self.assertEqual(self.service.restore_tag("juru,chihan"), "巨乳,痴汉")

    def test_safe_filename(self):
        self.assertEqual(self.service.safe_filename("a/b:c*?.pdf"), "a_b_c__")
        self.assertEqual(self.service.safe_filename(""), "untitled")


if __name__ == "__main__":
    unittest.main()

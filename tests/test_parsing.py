import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_jmcomic.core.parsing import (  # noqa: E402
    extract_id,
    format_index_ranges,
    format_index_token,
    parse_positive_page,
)


class ParsingTest(unittest.TestCase):
    def test_parse_positive_page(self):
        self.assertEqual(parse_positive_page("2"), 2)
        for token in ("0", "-1", "abc", "1.5", ""):
            with self.assertRaises(ValueError):
                parse_positive_page(token)

    def test_extract_id(self):
        self.assertEqual(extract_id("JM123456"), "123456")
        self.assertEqual(extract_id("abc"), None)

    def test_format_index_ranges(self):
        self.assertEqual(format_index_ranges([1, 2, 3, 5, 7, 8]), "1-3,5,7-8")
        self.assertEqual(format_index_ranges([]), "")
        self.assertEqual(format_index_token([31, 35, 40]), "31,35,40")


if __name__ == "__main__":
    unittest.main()

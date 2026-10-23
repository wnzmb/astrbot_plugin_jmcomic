import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_jmcomic.core.models import PageUnit  # noqa: E402
from astrbot_plugin_jmcomic.services.selection import SelectionService  # noqa: E402


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self.service = SelectionService(None, None, None)
        self.chapters = [object() for _ in range(10)]

    def test_parse_download_args(self):
        self.assertEqual(
            self.service.parse_download_args(["jm", "123", "-latest", "5"]),
            ("123", "latest:5"),
        )
        self.assertEqual(
            self.service.parse_download_args(["jm", "123", "-c", "31-40"]),
            ("123", "31-40"),
        )
        with self.assertRaises(ValueError):
            self.service.parse_download_args(
                ["jm", "123", "-c", "1-3", "-latest", "2"]
            )
        with self.assertRaises(ValueError):
            self.service.parse_download_args(["jm", "123", "-latest", "0"])

    def test_normalize_target(self):
        self.assertEqual(self.service.normalize_target("JM123"), ("", "123"))
        self.assertEqual(self.service.normalize_target("a123"), ("a", "123"))
        self.assertEqual(self.service.normalize_target("p123"), ("p", "123"))
        self.assertEqual(self.service.normalize_target("-1"), ("", "-1"))

    def test_page_ranges(self):
        notes = []
        self.assertEqual(self.service.parse_page_range("201-400", notes), (201, 400))
        self.assertEqual(self.service.parse_page_range("201-", notes), (201, None))
        self.assertEqual(self.service.parse_page_range("5", notes), (5, 5))
        self.assertIsNone(self.service.parse_page_range("0", notes))
        self.assertEqual(
            self.service.merge_page_ranges([(1, 5), (6, 10), (20, None)]),
            [(1, 10), (20, None)],
        )

    def test_select_chapters(self):
        selected, invalid, notes = self.service.select_chapters(self.chapters, "2-3")
        self.assertEqual([item[0] for item in selected], [2, 3])
        self.assertEqual(invalid, [])
        self.assertEqual(notes, [])

        selected, _invalid, _notes = self.service.select_chapters(
            self.chapters, "5:1-5,5:10-15"
        )
        self.assertEqual([item[0] for item in selected], [5, 5])
        self.assertEqual(selected[0][2], (1, 5))
        self.assertEqual(selected[1][2], (10, 15))

        selected, invalid, _notes = self.service.select_chapters(
            self.chapters, "latest:3"
        )
        self.assertEqual([item[0] for item in selected], [8, 9, 10])
        self.assertEqual(invalid, [])

        selected, invalid, _notes = self.service.select_chapters(
            self.chapters, "latest:20"
        )
        self.assertEqual([item[0] for item in selected], list(range(1, 11)))
        self.assertEqual(invalid, [])

    def test_pack_units(self):
        units = [
            PageUnit(1, None, 1, 5, 5),
            PageUnit(2, None, 1, 5, 5),
            PageUnit(3, None, 1, 5, 5),
        ]
        groups = self.service.pack_units(units, max_pages=10)
        self.assertEqual([[u.index for u in group] for group in groups], [[1, 2], [3]])
        groups = self.service.pack_units(units, max_units=2)
        self.assertEqual([[u.index for u in group] for group in groups], [[1, 2], [3]])


if __name__ == "__main__":
    unittest.main()

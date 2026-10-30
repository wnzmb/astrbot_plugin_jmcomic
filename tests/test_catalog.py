import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_jmcomic.services.catalog import (  # noqa: E402
    CatalogService,
    get_album_detail_compat,
)


class RedirectAwareClient:
    """模拟 jmcomic 2.7.10+ 的 API 客户端，记录 allow_redirect 传参。"""

    def __init__(self):
        self.calls = []

    def get_album_detail(self, album_id, allow_redirect=True):
        self.calls.append((str(album_id), allow_redirect))
        return f"album-{album_id}"


class LegacyClient:
    """模拟旧版库或 HTML 客户端：没有 allow_redirect 参数。"""

    def __init__(self):
        self.calls = []

    def get_album_detail(self, album_id):
        self.calls.append((str(album_id), None))
        return f"legacy-{album_id}"


class FakeAlbum:
    def __init__(self, album_id, series_id=None, authors=None, tags=None, photo_ids=()):
        self.album_id = str(album_id)
        if series_id is not None:
            self.series_id = str(series_id)
        self.authors = list(authors or [])
        self.tags = list(tags or [])
        self._photos = [SimpleNamespace(photo_id=str(pid)) for pid in photo_ids]

    def __iter__(self):
        return iter(self._photos)


class AlbumRedirectCompatTest(unittest.TestCase):
    def test_disables_redirect_for_new_client(self):
        client = RedirectAwareClient()
        result = get_album_detail_compat(client, "275013")
        self.assertEqual(result, "album-275013")
        self.assertEqual(client.calls, [("275013", False)])

    def test_falls_back_when_client_lacks_parameter(self):
        client = LegacyClient()
        result = get_album_detail_compat(client, "275013")
        self.assertEqual(result, "legacy-275013")
        self.assertEqual(client.calls, [("275013", None)])


class ChapterAlbumDetectionTest(unittest.TestCase):
    def test_series_id_marks_chapter_album(self):
        # 章节 ID：series_id 指向父本，即使 authors/tags 正常也能识别
        album = FakeAlbum(
            "275013", series_id="43164", authors=["甘诘留太"], photo_ids=["275013"]
        )
        self.assertTrue(CatalogService.looks_like_chapter_album(album))

    def test_normal_album_series_id_equals_album_id(self):
        album = FakeAlbum(
            "43164", series_id="43164", authors=["甘诘留太"], photo_ids=["43164"]
        )
        self.assertFalse(CatalogService.looks_like_chapter_album(album))

    def test_single_chapter_album_with_series_zero(self):
        album = FakeAlbum("1484418", series_id="0", photo_ids=["1484418"])
        self.assertFalse(CatalogService.looks_like_chapter_album(album))

    def test_legacy_album_without_series_id_uses_old_heuristics(self):
        album = FakeAlbum(
            "275013", authors=["filename"], tags=["filename"], photo_ids=["43164"]
        )
        self.assertTrue(CatalogService.looks_like_chapter_album(album))

    def test_none_album_is_not_chapter(self):
        self.assertFalse(CatalogService.looks_like_chapter_album(None))

    def test_parent_id_prefers_series_id(self):
        album = FakeAlbum("275013", series_id="43164", photo_ids=["275013"])
        self.assertEqual(
            CatalogService.chapter_album_parent_id(album, "275013"), "43164"
        )

    def test_parent_id_falls_back_to_first_photo(self):
        album = FakeAlbum("275013", series_id="0", photo_ids=["43164", "274971"])
        self.assertEqual(
            CatalogService.chapter_album_parent_id(album, "275013"), "43164"
        )

    def test_parent_id_empty_for_normal_album(self):
        album = FakeAlbum("43164", series_id="43164", photo_ids=["43164"])
        self.assertEqual(CatalogService.chapter_album_parent_id(album, "43164"), "")


if __name__ == "__main__":
    unittest.main()

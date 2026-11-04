"""jmcomic 上游契约测试：升级 jmcomic 后先跑这个文件。

只断言插件真正依赖的接口、字段与语义，不访问网络。
任何一条失败，都说明升级会波及插件，必须同步改造后再发版。
"""

import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

import jmcomic  # noqa: E402


def _params(func):
    return inspect.signature(func).parameters


class UpstreamContractTest(unittest.TestCase):
    # ---------- 详情接口（v0.7.11 allow_redirect 兼容依赖） ----------
    def test_album_detail_accepts_allow_redirect(self):
        for cls in (jmcomic.JmApiClient, jmcomic.JmHtmlClient):
            self.assertIn(
                "allow_redirect",
                _params(cls.get_album_detail),
                f"{cls.__name__}.get_album_detail 缺少 allow_redirect",
            )

    def test_photo_detail_signature(self):
        for cls in (jmcomic.JmApiClient, jmcomic.JmHtmlClient):
            params = _params(cls.get_photo_detail)
            self.assertIn("fetch_album", params, cls.__name__)
            self.assertIn("fetch_scramble_id", params, cls.__name__)

    def test_album_entity_fields(self):
        params = _params(jmcomic.JmAlbumDetail.__init__)
        for name in ("series_id", "is_favorite", "liked", "page_count"):
            self.assertIn(name, params, f"JmAlbumDetail 缺少字段 {name}")

    # ---------- 下载与取消（v0.7.11 DownloadControl 依赖） ----------
    def test_download_async_accepts_control(self):
        for func in (jmcomic.download_album_async, jmcomic.download_photo_async):
            self.assertIn("control", _params(func), func.__name__)

    def test_download_control_api(self):
        control = jmcomic.DownloadControl()
        self.assertFalse(control.is_cancelled)
        self.assertTrue(control.cancel("contract"))
        self.assertTrue(control.is_cancelled)

    def test_cancelled_exception_exists(self):
        self.assertTrue(issubclass(jmcomic.DownloadCancelledException, BaseException))

    def test_async_downloader_hooks(self):
        for name in ("before_album", "before_photo", "after_image", "after_album"):
            self.assertTrue(
                hasattr(jmcomic.JmAsyncDownloader, name),
                f"JmAsyncDownloader 缺少钩子 {name}",
            )

    # ---------- 客户端内部细节（签到兼容路径依赖） ----------
    def test_api_client_checkin_internals(self):
        for name in ("req_api", "API_DAILY_CHK", "DAILY_ALREADY_CHECKED_IN_KEYWORDS"):
            self.assertTrue(
                hasattr(jmcomic.JmApiClient, name),
                f"JmApiClient 缺少 {name}，签到兼容路径会失效",
            )

    def test_daily_checkin_resp_codes(self):
        self.assertEqual(jmcomic.JmDailyCheckinResp.CODE_SUCCESS, 0)
        self.assertEqual(jmcomic.JmDailyCheckinResp.CODE_ALREADY_CHECKED_IN, 1)

    def test_crypto_tool_decode(self):
        self.assertTrue(callable(jmcomic.JmCryptoTool.decode_resp_data))

    # ---------- 配置构造（v0.7.13 默认值合并依赖） ----------
    def test_option_construct_merges_defaults(self):
        option = jmcomic.JmOption.construct({"client": {"retry_times": 2}})
        # 显式覆盖生效
        self.assertEqual(option.client.retry_times, 2)
        # 未写的键由默认配置补齐（深度合并）
        self.assertTrue(hasattr(option, "dir_rule"))
        self.assertTrue(option.download.src_dict.get("cache") is True)

    def test_module_config_domains(self):
        module_config = jmcomic.JmModuleConfig
        self.assertIsInstance(module_config.DOMAIN_API_LIST, list)
        self.assertTrue(
            hasattr(module_config, "DOMAIN_HTML_LIST")
            or hasattr(module_config, "DOMAIN_HTML")
        )
        self.assertTrue(hasattr(module_config, "DOMAIN_API_UPDATED_LIST"))

    # ---------- 登录态（v0.7.15 起依赖 2.7.11 的 auth 写回） ----------
    def test_option_auth_login_helper(self):
        option = jmcomic.JmOption.construct({})
        if not hasattr(option, "login_with_option_auth"):
            self.skipTest("jmcomic < 2.7.11 没有 auth 登录接口，插件会退回 client.login()")
        self.assertTrue(hasattr(option, "auth"))
        self.assertTrue(callable(option.login_with_option_auth))

    def test_async_client_registry_has_async_api(self):
        registry = jmcomic.JmModuleConfig.REGISTRY_ASYNC_CLIENT
        self.assertIn("async_api", registry, "下载器依赖 async_api 客户端")

    def test_async_html_registered(self):
        registry = jmcomic.JmModuleConfig.REGISTRY_ASYNC_CLIENT
        if "async_html" not in registry:
            self.skipTest("jmcomic < 2.7.11 没有 async_html")
        self.assertIn("async_html", registry)

    # ---------- 其它已用接口 ----------
    def test_favorite_and_comment_apis(self):
        for name in (
            "favorite_folder",
            "favorite_folder_gen",
            "album_pagination",
            "add_favorite_album",
            "delete_favorite_album",
            "search_site",
            "search_author",
            "search_tag",
            "search_actor",
            "search_work",
            "download_album_cover",
            "get_domain_list",
        ):
            self.assertTrue(
                hasattr(jmcomic.JmApiClient, name),
                f"JmApiClient 缺少 {name}",
            )


if __name__ == "__main__":
    unittest.main()

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_jmcomic.core.config import PluginSettings  # noqa: E402


class ConfigTest(unittest.TestCase):
    def test_new_options_have_expected_defaults(self):
        settings = PluginSettings.from_config({}, ROOT)
        self.assertEqual(settings.request_timeout, 15)
        self.assertEqual(settings.client_retry_times, 5)
        self.assertEqual(settings.download_retry_times, 3)
        self.assertEqual(settings.download_retry_interval, 3.0)
        self.assertEqual(settings.login_ttl_minutes, 10)
        self.assertEqual(settings.max_batch_targets, 0)
        self.assertTrue(settings.send_cover)
        self.assertEqual(settings.confirm_timeout, 30)
        self.assertEqual(settings.subscription_push_limit, 10)
        self.assertTrue(settings.page_turn_keyword_enabled)
        self.assertTrue(settings.include_info_page)
        self.assertEqual(settings.send_interval, 5.0)
        self.assertEqual(settings.pdf_page_limit, 100)
        self.assertEqual(settings.zip_page_limit, 200)
        self.assertEqual(settings.history_max, 200)

    def test_history_max_zero_keeps_unlimited(self):
        settings = PluginSettings.from_config({"history_max": 0}, ROOT)
        self.assertEqual(settings.history_max, 0)

    def test_storage_dir_overrides_download_and_zip_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = PluginSettings.from_config({"storage_dir": tmp}, ROOT)

            self.assertEqual(settings.download_base_dir, Path(tmp) / "downloads")
            self.assertEqual(settings.zip_base_dir, Path(tmp) / "zips")
            # 状态文件仍留在默认 plugin_data 目录
            self.assertEqual(settings.history_file, settings.data_dir / "history.json")

    def test_default_storage_dir_uses_plugin_data(self):
        settings = PluginSettings.from_config({}, ROOT)

        self.assertEqual(settings.download_base_dir.name, "downloads")
        self.assertEqual(settings.download_base_dir.parent.name, "astrbot_plugin_jmcomic")

    def test_prepare_directories_with_custom_storage_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp) / "data" / "plugins" / "astrbot_plugin_jmcomic"
            plugin_dir.mkdir(parents=True)
            settings = PluginSettings.from_config(
                {"storage_dir": str(Path(tmp) / "JMStorage")}, plugin_dir
            )

            settings.prepare_directories()

            self.assertTrue(settings.download_base_dir.is_dir())
            self.assertTrue(settings.zip_base_dir.is_dir())
            # storage_dir 生效时也不能漏建默认状态目录
            self.assertTrue(settings.history_file.is_file())
            self.assertTrue(settings.subscription_file.is_file())

    def test_refresh_option_rebuilds_on_frozen_dataclass(self):
        """真实 PluginSettings 必须能重建 option（旧代码抛 FrozenInstanceError）。"""
        settings = PluginSettings.from_config({}, ROOT)
        old_option = settings.option

        new_option = settings.refresh_option()

        self.assertIs(settings.option, new_option)
        self.assertIsNot(old_option, new_option)

    def test_page_limits_are_clamped_to_500(self):
        settings = PluginSettings.from_config(
            {
                "pdf_page_limit": 9999,
                "zip_page_limit": 9999,
            },
            ROOT,
        )
        self.assertEqual(settings.pdf_page_limit, 500)
        self.assertEqual(settings.zip_page_limit, 500)

    def test_new_options_are_parsed_and_clamped(self):
        settings = PluginSettings.from_config(
            {
                "request_timeout": 999,
                "client_retry_times": 999,
                "download_retry_times": 999,
                "download_retry_interval": 999,
                "login_ttl_minutes": 999,
                "max_batch_targets": 999,
                "send_cover": False,
                "confirm_timeout": 999,
                "subscription_push_limit": 999,
                "page_turn_keyword_enabled": False,
                "include_info_page": False,
            },
            ROOT,
        )
        self.assertEqual(settings.request_timeout, 60)
        self.assertEqual(settings.client_retry_times, 10)
        self.assertEqual(settings.download_retry_times, 10)
        self.assertEqual(settings.download_retry_interval, 30.0)
        self.assertEqual(settings.login_ttl_minutes, 120)
        self.assertEqual(settings.max_batch_targets, 50)
        self.assertFalse(settings.send_cover)
        self.assertEqual(settings.confirm_timeout, 120)
        self.assertEqual(settings.subscription_push_limit, 30)
        self.assertFalse(settings.page_turn_keyword_enabled)
        self.assertFalse(settings.include_info_page)


if __name__ == "__main__":
    unittest.main()

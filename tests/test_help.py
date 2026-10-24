import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_jmcomic.ui.help import build_help_text  # noqa: E402


class HelpTest(unittest.TestCase):
    @staticmethod
    def settings(**overrides):
        values = {
            "client_type": "api",
            "per_page": 10,
            "page_turn_timeout": 60,
            "cache_days": 7,
            "pdf_page_limit": 200,
            "zip_group_size": 5,
            "zip_page_hint": lambda: "页数不限",
            "daily_file_limit": 0,
            "hourly_file_limit": 0,
            "send_cover": True,
            "include_info_page": True,
            "page_turn_keyword_enabled": True,
            "confirm_timeout": 30,
            "max_batch_targets": 0,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    @staticmethod
    def quota():
        return SimpleNamespace(limit_text=lambda value: "不限" if value == 0 else str(value))

    def test_help_reflects_disabled_features(self):
        text = build_help_text(
            self.settings(
                send_cover=False,
                include_info_page=False,
                page_turn_keyword_enabled=False,
                confirm_timeout=45,
                max_batch_targets=20,
            ),
            self.quota(),
        )
        self.assertIn("详情封面：不发送（配置已关闭）", text)
        self.assertIn("已关闭信息页/漫画信息.txt", text)
        self.assertIn("纯文本“下一页/下页”已关闭", text)
        self.assertIn("45秒", text)
        self.assertIn("每批最多 20 个目标", text)


if __name__ == "__main__":
    unittest.main()

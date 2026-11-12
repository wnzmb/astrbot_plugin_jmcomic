import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_jmcomic.handlers.system import (  # noqa: E402
    _html_domain_fallback,
)


class HtmlDomainFallbackTest(unittest.TestCase):
    def test_prefers_domain_html_list(self):
        module_config = SimpleNamespace(
            DOMAIN_HTML_LIST=["a.example", "b.example"],
            DOMAIN_HTML="legacy.example",
        )

        self.assertEqual(
            _html_domain_fallback(module_config),
            ["a.example", "b.example"],
        )

    def test_reads_legacy_string_before_2_7_12(self):
        module_config = SimpleNamespace(
            DOMAIN_HTML_LIST=None,
            DOMAIN_HTML="legacy.example",
        )

        self.assertEqual(_html_domain_fallback(module_config), ["legacy.example"])

    def test_2_7_12_without_domain_html_returns_empty(self):
        """2.7.12 移除了 DOMAIN_HTML，缺失时不能抛 AttributeError。"""
        module_config = SimpleNamespace(DOMAIN_HTML_LIST=None)

        self.assertEqual(_html_domain_fallback(module_config), [])

    def test_none_module_config(self):
        self.assertEqual(_html_domain_fallback(None), [])


if __name__ == "__main__":
    unittest.main()

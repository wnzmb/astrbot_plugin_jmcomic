import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_jmcomic.core.state import PluginState  # noqa: E402
from astrbot_plugin_jmcomic.services.pagination import PageSessionService  # noqa: E402


class Event:
    unified_msg_origin = "webchat:test"

    def get_sender_id(self):
        return "user-1"


class PaginationTest(unittest.TestCase):
    def test_remember_and_timeout(self):
        settings = SimpleNamespace(page_turn_timeout=60)
        state = PluginState()
        service = PageSessionService(settings, state)
        event = Event()
        service.remember(event, "/jmlist 1 2", 1, 3)
        session = service.get(event)
        self.assertIsNotNone(session)
        self.assertEqual(session["next_command"], "/jmlist 1 2")
        service.clear(event)
        self.assertIsNone(service.get(event))

    def test_last_page_marker(self):
        settings = SimpleNamespace(page_turn_timeout=60)
        state = PluginState()
        service = PageSessionService(settings, state)
        event = Event()
        service.remember(event, "/jmhis 2", 1, 1)
        session = service.get(event)
        self.assertTrue(session["at_last_page"])
        self.assertEqual(session["next_command"], "")

    def test_expired(self):
        settings = SimpleNamespace(page_turn_timeout=1)
        state = PluginState()
        service = PageSessionService(settings, state)
        event = Event()
        service.remember(event, "/jmhis 2", 1, 3)
        key = service.page_session_key(event)
        state.page_sessions[key]["expires_at"] = time.monotonic() - 1
        self.assertIsNone(service.get(event))

    def test_handler_name(self):
        self.assertEqual(PageSessionService.page_handler_name("jmlist"), "jm_chapter_list")
        self.assertEqual(PageSessionService.page_handler_name("jmnext"), None)


if __name__ == "__main__":
    unittest.main()

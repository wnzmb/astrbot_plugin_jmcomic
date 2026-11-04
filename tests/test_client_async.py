import asyncio
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_jmcomic.core.client import JmClientManager  # noqa: E402


class FakeOption:
    def __init__(self):
        self.clients = []

    def new_jm_client(self):
        client = object()
        self.clients.append(client)
        return client


class FakeSettings:
    login_ttl_minutes = 10
    username = ""
    password = ""

    def __init__(self):
        self.option = FakeOption()

    def refresh_option(self):
        return self.option


class ClientAsyncTest(unittest.TestCase):
    def test_get_client_async_runs_in_thread(self):
        """登录可能包含网络请求，异步入口必须把它挪出事件循环。"""
        manager = JmClientManager(FakeSettings())
        thread_names = []
        original = manager.get_client

        def wrapper():
            thread_names.append(threading.current_thread().name)
            return original()

        manager.get_client = wrapper

        client = asyncio.run(manager.get_client_async())

        self.assertIsNotNone(client)
        self.assertEqual(len(thread_names), 1)
        self.assertNotEqual(thread_names[0], threading.current_thread().name)


if __name__ == "__main__":
    unittest.main()

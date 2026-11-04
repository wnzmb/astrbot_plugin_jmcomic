"""JmClientManager 登录路径：2.7.11 auth 写回与旧版兼容。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_jmcomic.core.client import JmClientManager  # noqa: E402


class FakeClient:
    def __init__(self):
        self.login_calls = []

    def login(self, username, password):
        self.login_calls.append((username, password))


class FakeAuth(dict):
    pass


class AuthOption:
    """模拟 2.7.11+：有 auth 与 login_with_option_auth。"""

    def __init__(self, fail=None):
        self.auth = FakeAuth()
        self.fail = fail
        self.auth_calls = []
        self.created = []

    def login_with_option_auth(self, force=False):
        self.auth_calls.append(force)
        if self.fail is not None:
            raise self.fail
        self.auth["cookies"] = {"AVS": "x"}
        self.auth["user_id"] = "100"

    def new_jm_client(self):
        client = FakeClient()
        self.created.append(client)
        return client


class LegacyOption:
    """模拟 2.7.10 及以前：只有 client.login()。"""

    def __init__(self):
        self.created = []

    def new_jm_client(self):
        client = FakeClient()
        self.created.append(client)
        return client


class FakeSettings:
    def __init__(self, option, username="user", password="pass"):
        self.option = option
        self.username = username
        self.password = password
        self.login_ttl_minutes = 10

    def refresh_option(self):
        return self.option


class ClientLoginTest(unittest.TestCase):
    def test_uses_auth_helper_and_rebuilds_client(self):
        option = AuthOption()
        manager = JmClientManager(FakeSettings(option))

        client = manager.get_client()

        self.assertEqual(option.auth_calls, [True])
        self.assertEqual(option.auth["username"], "user")
        self.assertEqual(option.auth["password"], "pass")
        # 登录后从 option 重建客户端，确保带最新 cookies
        self.assertIs(client, option.created[-1])
        self.assertIs(manager._cached_client, client)
        self.assertEqual(len(option.created), 2)

    def test_auth_helper_failure_raises_friendly_error(self):
        option = AuthOption(fail=RuntimeError("bad password"))
        manager = JmClientManager(FakeSettings(option))

        with self.assertRaises(Exception) as ctx:
            manager.get_client()

        self.assertIn("登录失败", str(ctx.exception))
        self.assertIn("bad password", str(ctx.exception))

    def test_legacy_client_login_is_used(self):
        option = LegacyOption()
        manager = JmClientManager(FakeSettings(option))

        client = manager.get_client()

        self.assertEqual(len(option.created), 1)
        self.assertIs(client, option.created[0])
        self.assertEqual(client.login_calls, [("user", "pass")])

    def test_no_credentials_skips_login(self):
        option = AuthOption()
        manager = JmClientManager(FakeSettings(option, username="", password=""))

        client = manager.get_client()

        self.assertEqual(option.auth_calls, [])
        self.assertEqual(len(option.created), 1)
        self.assertIs(client, option.created[0])

    def test_refresh_client_relogs_in(self):
        option = AuthOption()
        manager = JmClientManager(FakeSettings(option))
        manager.get_client()

        manager.refresh_client()

        self.assertEqual(option.auth_calls, [True, True])

    def test_configured_username_prefers_auth(self):
        option = AuthOption()
        option.auth["username"] = "from-auth"
        manager = JmClientManager(FakeSettings(option, username="", password=""))

        self.assertEqual(manager.configured_username(), "from-auth")

    def test_configured_username_falls_back_to_login_plugin(self):
        option = LegacyOption()
        option.plugins = type(
            "Plugins",
            (),
            {
                "after_init": [
                    {"plugin": "login", "kwargs": {"username": "from-plugin"}}
                ]
            },
        )()
        manager = JmClientManager(FakeSettings(option, username="", password=""))

        self.assertEqual(manager.configured_username(), "from-plugin")


if __name__ == "__main__":
    unittest.main()

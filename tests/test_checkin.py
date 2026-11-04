import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from jmcomic import JmDailyCheckinResp  # noqa: E402

from astrbot_plugin_jmcomic.handlers.account import AccountHandler  # noqa: E402
from astrbot_plugin_jmcomic.services.checkin import CheckinService  # noqa: E402


class FakeApiResp:
    """模拟 JmApiResp 的最小接口。"""

    def __init__(self, payload, ts="1700000000"):
        self.payload = payload
        self.ts = ts
        self.resp = None

    def json(self):
        return self.payload


class FakeClient:
    """模拟 API 客户端，只实现签到相关接口。"""

    API_DAILY_CHK = "/daily_chk"
    _UNSET = object()

    def __init__(self, checkin_payload, daily_payload=None, user_id="100",
                 library_result=None, library_error=_UNSET):
        self._user_id = user_id
        self.checkin_payload = checkin_payload
        self.daily = daily_payload or {"daily_id": "daily-1"}
        self.checkin_calls = 0
        self.daily_calls = 0
        self.library_calls = 0
        self.library_result = library_result
        # 默认模拟 2.7.10 对空 data 的解码失败，让插件走兼容路径
        self.library_error = (
            RuntimeError("签到失败：[]")
            if library_error is FakeClient._UNSET
            else library_error
        )

    def get_daily(self, user_id=None):
        self.daily_calls += 1
        return SimpleNamespace(model_data=self.daily)

    def req_api(self, url, get=True, data=None):
        self.checkin_calls += 1
        return FakeApiResp(self.checkin_payload)

    def daily_checkin(self, daily_id=None, user_id=None):
        self.library_calls += 1
        if self.library_error is not None:
            raise self.library_error
        return self.library_result


class FakeAccess:
    def check_permission(self, event):
        return True

    def is_owner(self, event):
        return True


class FakeClientManager:
    def __init__(self, client):
        self.client = client

    def get_client(self):
        return self.client

    def is_auth_error(self, exc):
        return False

    def refresh_client(self):
        return self.client

    def configured_username(self):
        return "100"


class FakeMessaging:
    def plain(self, event, text):
        return text


class CheckinServiceTest(unittest.TestCase):
    def test_empty_data_is_treated_as_already_signed(self):
        service = CheckinService(settings=None)
        client = FakeClient({"code": 200, "data": []})

        resp = service.daily_checkin(client, "daily-1")

        self.assertEqual(resp.code, JmDailyCheckinResp.CODE_ALREADY_CHECKED_IN)
        self.assertIn("今日可能已签到", resp.msg)
        # 先尝试库实现，失败后回退到兼容路径
        self.assertEqual(client.library_calls, 1)
        self.assertEqual(client.checkin_calls, 1)

    def test_library_success_is_used_directly(self):
        service = CheckinService(settings=None)
        library_resp = JmDailyCheckinResp(None, JmDailyCheckinResp.CODE_SUCCESS, "Jcoin:10")
        client = FakeClient({"code": 200, "data": []}, library_result=library_resp,
                            library_error=None)

        resp = service.daily_checkin(client, "daily-1")

        self.assertIs(resp, library_resp)
        self.assertEqual(client.library_calls, 1)
        self.assertEqual(client.checkin_calls, 0)

    def test_login_error_does_not_fall_back(self):
        service = CheckinService(settings=None)
        client = FakeClient(
            {"code": 200, "data": []},
            library_error=RuntimeError("签到需要传入 user_id 参数，或者先调用 login 方法"),
        )

        with self.assertRaises(RuntimeError):
            service.daily_checkin(client, "daily-1")

        self.assertEqual(client.library_calls, 1)
        self.assertEqual(client.checkin_calls, 0)

    def test_empty_data_with_error_message_still_fails(self):
        service = CheckinService(settings=None)
        client = FakeClient({"code": 200, "data": [], "errorMsg": "服务异常"})

        with self.assertRaises(RuntimeError):
            service.daily_checkin(client, "daily-1")

    def test_simplified_already_signed_message(self):
        service = CheckinService(settings=None)
        client = FakeClient({"code": 200, "data": {"msg": "今天已经签到过了"}})

        resp = service.daily_checkin(client, "daily-1")

        self.assertEqual(resp.code, JmDailyCheckinResp.CODE_ALREADY_CHECKED_IN)

    def test_success_message(self):
        service = CheckinService(settings=None)
        client = FakeClient({"code": 200, "data": {"msg": "Jcoin:10 EXP:10"}})

        resp = service.daily_checkin(client, "daily-1")

        self.assertEqual(resp.code, JmDailyCheckinResp.CODE_SUCCESS)


class CheckinHandlerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(
            checkin_file=Path(self.temp_dir.name) / "checkin.json",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    async def collect(self, handler):
        return [chunk async for chunk in handler.daily(object())]

    async def test_already_signed_calendar_skips_checkin_request(self):
        client = FakeClient(
            {"code": 200, "data": []},
            {"daily_id": "daily-1", "today_signed": True, "oldStep": 5},
        )
        handler = AccountHandler(
            self.settings,
            FakeAccess(),
            FakeClientManager(client),
            CheckinService(self.settings),
            FakeMessaging(),
        )

        replies = await self.collect(handler)

        self.assertEqual(client.checkin_calls, 0)
        self.assertIn("今日已签到", replies[-1])
        self.assertIn("5 天", replies[-1])

    async def test_empty_data_after_checkin_reports_already_signed(self):
        client = FakeClient(
            {"code": 200, "data": []},
            {"daily_id": "daily-1", "oldStep": 4},
        )
        handler = AccountHandler(
            self.settings,
            FakeAccess(),
            FakeClientManager(client),
            CheckinService(self.settings),
            FakeMessaging(),
        )

        replies = await self.collect(handler)

        self.assertEqual(client.checkin_calls, 1)
        self.assertIn("今日已签到", replies[-1])
        self.assertTrue(self.settings.checkin_file.exists())


if __name__ == "__main__":
    unittest.main()

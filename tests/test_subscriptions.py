import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_jmcomic.services.subscriptions import (  # noqa: E402
    FAIL_BACKOFF_CAP_SECONDS,
    SubscriptionService,
)


class FakeSettings:
    def __init__(self, path, interval=3600):
        self.subscription_file = path
        self.subscribe_interval = interval
        self.subscription_push_limit = 10
        self.convert_title = False


class FakeClient:
    def __init__(self, album=None, error=None):
        self.album = album
        self.error = error
        self.calls = 0

    def get_client(self):
        return self

    def get_album_detail(self, album_id):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.album


class FakeState:
    subscription_lock = None


class FakeText:
    def safe_text(self, text, *_args):
        return text


class FakeCatalog:
    @staticmethod
    def photo_position(_photo, index):
        return index


class FakeContext:
    def __init__(self):
        self.sent = []

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))
        return True


def _album(*photo_ids):
    return [
        SimpleNamespace(photo_id=str(photo_id), name=f"第{index}话")
        for index, photo_id in enumerate(photo_ids, 1)
    ]


class SubscriptionBackoffTest(unittest.TestCase):
    def _service(self, tmp, client):
        path = Path(tmp) / "subscriptions.json"
        path.write_text(
            json.dumps(
                {
                    "webchat:test": {
                        "43164": {"title": "测试", "photo_ids": ["1"]}
                    }
                }
            ),
            encoding="utf-8",
        )
        return (
            SubscriptionService(
                FakeSettings(path),
                client,
                FakeText(),
                FakeCatalog(),
                FakeState(),
                FakeContext(),
            ),
            path,
        )

    def _check(self, service):
        # 退避逻辑不依赖真实等待，patch 掉每本之间的 2 秒间隔
        with mock.patch(
            "astrbot_plugin_jmcomic.services.subscriptions.asyncio.sleep",
            new=mock.AsyncMock(),
        ):
            asyncio.run(service.check_once())

    def test_failure_sets_backoff_and_skips_next_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(error=RuntimeError("404"))
            service, path = self._service(tmp, client)

            self._check(service)
            info = json.loads(path.read_text(encoding="utf-8"))["webchat:test"]["43164"]
            self.assertEqual(info["fails"], 1)
            self.assertGreater(info["next_check_at"], time.time())

            # 退避窗口内不再请求
            self._check(service)
            self.assertEqual(client.calls, 1)

            delay = info["next_check_at"] - time.time()
            self.assertGreater(delay, 0)
            self.assertLessEqual(delay, FAIL_BACKOFF_CAP_SECONDS)

    def test_success_clears_failure_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(album=_album(1, 2))
            service, path = self._service(tmp, client)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["webchat:test"]["43164"].update(
                {"fails": 3, "next_check_at": 0}
            )
            path.write_text(json.dumps(data), encoding="utf-8")

            self._check(service)

            info = json.loads(path.read_text(encoding="utf-8"))["webchat:test"]["43164"]
            self.assertNotIn("fails", info)
            self.assertNotIn("next_check_at", info)
            self.assertEqual(info["photo_ids"], ["1", "2"])
            self.assertEqual(len(service.context.sent), 1)

    def test_backoff_cap_does_not_exceed_24h(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(error=RuntimeError("404"))
            service, path = self._service(tmp, client)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["webchat:test"]["43164"]["fails"] = 30
            path.write_text(json.dumps(data), encoding="utf-8")

            self._check(service)

            info = json.loads(path.read_text(encoding="utf-8"))["webchat:test"]["43164"]
            self.assertLessEqual(
                info["next_check_at"] - time.time(), FAIL_BACKOFF_CAP_SECONDS
            )


if __name__ == "__main__":
    unittest.main()

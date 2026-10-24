import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_jmcomic.services.download_runner import DownloadRunner  # noqa: E402


class Selection:
    @staticmethod
    def normalize_target(raw):
        return None, raw


class Messaging:
    @staticmethod
    def plain(_event, text):
        return text


class DownloadRunnerTest(unittest.TestCase):
    def test_batch_limit_rejects_before_download(self):
        runner = DownloadRunner(
            SimpleNamespace(max_batch_targets=1),
            None,
            None,
            None,
            Messaging(),
            None,
            None,
            None,
            Selection(),
            None,
            None,
        )

        async def collect():
            return [
                message
                async for message in runner.run_download(object(), "1,2", "pdf")
            ]

        messages = asyncio.run(collect())
        self.assertEqual(len(messages), 1)
        self.assertIn("一次最多处理 1 个目标", messages[0])


if __name__ == "__main__":
    unittest.main()

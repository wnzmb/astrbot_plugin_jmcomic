"""文件发送限额：读取/写入 send_limits.json，统计每日/每小时额度。"""

import json
from datetime import datetime, timedelta

from astrbot.api import logger


class FileQuotaService:
    def __init__(self, settings):
        self.settings = settings

    def load_send_times(self):
        try:
            data = json.loads(self.settings.send_state_file.read_text(encoding="utf-8"))
            times = data.get("times", []) if isinstance(data, dict) else []
            result = []
            for item in times:
                try:
                    result.append(datetime.strptime(str(item), "%Y-%m-%dT%H:%M:%S"))
                except Exception:
                    continue
            return result
        except Exception:
            return []

    def save_send_times(self, times):
        try:
            now = datetime.now()
            cutoff = now - timedelta(hours=24)
            times = sorted(t for t in times if t >= cutoff)
            payload = {"times": [t.strftime("%Y-%m-%dT%H:%M:%S") for t in times]}
            self.settings.send_state_file.write_text(json.dumps(payload), encoding="utf-8")
        except Exception as e:
            logger.debug(f"保存发送限额状态失败: {e}")

    def file_quota(self):
        """返回今日/最近1小时的文件发送额度（本地统计，只统计本插件）

        限额配置为 0 时表示不限制，remaining 返回 None。
        """
        now = datetime.now()
        times = self.load_send_times()
        hour_ago = now - timedelta(hours=1)
        hour_count = sum(1 for t in times if t >= hour_ago)
        day_count = sum(1 for t in times if t.date() == now.date())
        return {
            "hour_count": hour_count,
            "hour_remaining": (
                None if self.settings.hourly_file_limit <= 0
                else max(0, self.settings.hourly_file_limit - hour_count)
            ),
            "day_count": day_count,
            "day_remaining": (
                None if self.settings.daily_file_limit <= 0
                else max(0, self.settings.daily_file_limit - day_count)
            ),
        }

    @staticmethod
    def quota_reached(quota):
        """任一限额已用完；None 表示该限额不限制"""
        return any(
            quota[key] is not None and quota[key] <= 0
            for key in ("hour_remaining", "day_remaining")
        )

    @staticmethod
    def quota_display(count, limit):
        return f"{count}/不限" if limit <= 0 else f"{count}/{limit}"

    @staticmethod
    def limit_text(limit):
        return "不限" if limit <= 0 else str(limit)

    def record_file_send(self, count=1):
        now = datetime.now()
        times = self.load_send_times()
        times.extend([now] * max(0, count))
        self.save_send_times(times)

    def block_text(self, remaining_files=0):
        q = self.file_quota()
        if q["day_remaining"] is not None and q["day_remaining"] <= 0:
            return (
                f"⏳ 今日文件发送额度已用完（{q['day_count']}/{self.settings.daily_file_limit}）。\n"
                f"剩余 {remaining_files} 个文件未发送，请明天再试。\n"
                "已下载的图片会保留缓存，重新执行命令不会重新下载。"
            )
        if q["hour_remaining"] is not None and q["hour_remaining"] <= 0:
            return (
                f"⏳ 最近1小时文件发送额度已用完（{q['hour_count']}/{self.settings.hourly_file_limit}）。\n"
                f"剩余 {remaining_files} 个文件未发送，请至少等待约 1 小时后再试。\n"
                "已下载的图片会保留缓存，重新执行命令不会重新下载。"
            )
        return ""

    def status_text(self):
        q = self.file_quota()
        return (
            f"今日文件 {self.quota_display(q['day_count'], self.settings.daily_file_limit)}，"
            f"最近1小时 {self.quota_display(q['hour_count'], self.settings.hourly_file_limit)}"
        )

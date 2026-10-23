"""缓存维护命令：/jmdisk 磁盘统计、/jmclean 清理图片缓存。"""

import asyncio
import time

from astrbot.api.event import AstrMessageEvent

from ..core.downloader import ProgressDownloader


class MaintenanceHandler:
    def __init__(self, settings, access, maintenance, state, messaging):
        self.settings = settings
        self.access = access
        self.maintenance = maintenance
        self.state = state
        self.messaging = messaging

    def _allowed(self, event):
        return self.access.is_admin(event) or self.access.is_owner(event)

    def _active_album_ids(self):
        active = set(ProgressDownloader._album_dirs.keys())
        for info in self.state.active_downloads.values():
            if not isinstance(info, dict):
                continue
            album_id = str(info.get("album_id", "") or "")
            if album_id:
                active.add(album_id)
        return active

    @staticmethod
    def _clean_key(event, days):
        return (
            str(event.get_sender_id()),
            str(getattr(event, "unified_msg_origin", "") or ""),
            days,
        )

    async def disk(self, event: AstrMessageEvent):
        """查看下载缓存、ZIP/CSV 临时目录占用"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        if not self._allowed(event):
            yield self.messaging.plain(event, "⚠️ 只有管理员或号主可以使用 /jmdisk")
            return
        yield self.messaging.plain(event, "💾 正在统计磁盘占用...")
        try:
            report, usage = await asyncio.to_thread(self.maintenance.disk_report)
        except Exception as e:
            yield self.messaging.plain(event, f"❌ 统计失败：{e}")
            return

        lines = ["💾 缓存磁盘占用"]
        for item in report:
            if item["label"] == "下载缓存":
                lines.append(
                    f"\n📥 {item['label']}（{item['albums']} 个漫画目录）\n"
                    f"  图片：{item['images']} 个 / {self.maintenance.human_size(item['image_bytes'])}\n"
                    f"  metadata：{item['metadata']} 个 / {self.maintenance.human_size(item['metadata_bytes'])}\n"
                    f"  合计：{item['files']} 个文件 / {self.maintenance.human_size(item['bytes'])}"
                )
            else:
                lines.append(
                    f"\n🗂️ {item['label']}：{item['files']} 个 / "
                    f"{self.maintenance.human_size(item['bytes'])}"
                )
        lines.append(
            f"\n💽 磁盘剩余：{self.maintenance.human_size(usage.free)} / "
            f"{self.maintenance.human_size(usage.total)}"
        )
        lines.append("\n🧹 清理：/jmclean [天数|all]（保留 metadata.json）")
        yield self.messaging.plain(event, "\n".join(lines))

    async def clean(self, event: AstrMessageEvent):
        """清理下载缓存图片；默认清理 cache_days 天前，保留 metadata.json"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        if not self._allowed(event):
            yield self.messaging.plain(event, "⚠️ 只有管理员或号主可以使用 /jmclean")
            return

        args = event.message_str.split()
        days = self.settings.cache_days if self.settings.cache_days > 0 else None
        days_label = None
        if len(args) >= 2:
            token = str(args[1]).strip().lower()
            if token in ("all", "-all"):
                days = None
                days_label = "全部历史图片"
            elif token.isdigit() and int(token) > 0:
                days = int(token)
                days_label = f"{days} 天前"
            else:
                yield self.messaging.plain(
                    event,
                    "用法：/jmclean [天数|all]\n"
                    "示例：/jmclean 7、/jmclean all；不填时使用 cache_days。\n"
                    "只会删除图片，保留 metadata.json。",
                )
                return
        if days is None and days_label is None:
            yield self.messaging.plain(
                event,
                "cache_days=0（永久保留），请明确指定天数或 all：\n"
                "/jmclean 7  或  /jmclean all",
            )
            return
        if days_label is None:
            days_label = f"{days} 天前"

        key = self._clean_key(event, days)
        now = time.time()
        pending = self.state.pending_cleans.get(key)
        if pending and pending > now:
            self.state.pending_cleans.pop(key, None)
            active = self._active_album_ids()
            try:
                result = await asyncio.to_thread(
                    self.maintenance.clean, days, active
                )
            except Exception as e:
                yield self.messaging.plain(event, f"❌ 清理失败：{e}")
                return
            skipped = result.get("skipped_active") or []
            text = (
                f"🧹 已清理 {days_label}的图片：{result['files']} 个 / "
                f"{self.maintenance.human_size(result['bytes'])}。\n"
                "metadata.json 已保留，话页数缓存仍可用。"
            )
            if result.get("failed"):
                text += f"\n⚠️ 有 {result['failed']} 个文件删除失败，详见日志。"
            if skipped:
                text += f"\n⏭️ 跳过活动下载目录：{', '.join(skipped)}"
            yield self.messaging.plain(event, text)
            return

        # 第一次只预览，30 秒内再次发送相同命令才真正删除
        active = self._active_album_ids()
        try:
            preview = await asyncio.to_thread(
                self.maintenance.scan_clean, days, active
            )
        except Exception as e:
            yield self.messaging.plain(event, f"❌ 统计失败：{e}")
            return
        self.state.pending_cleans[key] = now + 30
        text = (
            f"🧹 准备清理 {days_label}的图片缓存\n"
            f"预计：{preview['files']} 个图片 / "
            f"{self.maintenance.human_size(preview['bytes'])}\n"
            "只会删除图片，metadata.json 会保留。"
        )
        skipped = preview.get("skipped_active") or []
        if skipped:
            text += f"\n⏭️ 活动下载目录将跳过：{', '.join(skipped)}"
        if not preview["files"]:
            text += "\n\n当前没有符合条件的图片。"
        text += "\n\n30 秒内再次发送相同命令确认清理。"
        yield self.messaging.plain(event, text)

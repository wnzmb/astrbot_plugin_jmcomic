"""PDF/ZIP 下载、停止与进度查询。"""

import asyncio

from astrbot.api.event import AstrMessageEvent

from ..core.downloader import ProgressDownloader
from ..ui.help import build_help_text


class DownloadHandler:
    def __init__(self, settings, access, selection, runner, state, progress, messaging, quota):
        self.settings = settings
        self.access = access
        self.selection = selection
        self.runner = runner
        self.state = state
        self.progress = progress
        self.messaging = messaging
        self.quota = quota

    async def download(self, event: AstrMessageEvent):
        """下载漫画/章节并发送 PDF，用法：/jm <id> [-c 章节]"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        args = event.message_str.split()

        # 不带参数或 help，输出帮助
        if len(args) < 2 or args[1].lower() == 'help':
            help_text = build_help_text(self.settings, self.quota)
            yield self.messaging.plain(event, help_text)
            return

        try:
            target, chapter_expr = self.selection.parse_download_args(args)
        except ValueError as e:
            yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jm <漫画id|章节id> [-c 章节|章节:起页-止页]")
            return
        if not target:
            yield self.messaging.plain(event, "用法：/jm <漫画id|章节id> [-c 章节|章节:起页-止页]")
            return
        async for message in self.consume(
            event, target, "pdf", chapter_expr
        ):
            yield message

    async def consume(self, event, target, output_format, chapter_expr):
        """包装下载生成器，登记当前任务供 /jmstop 取消。"""
        task = asyncio.current_task()
        user_id = str(event.get_sender_id())
        if task is not None:
            self.state.download_tasks.setdefault(user_id, set()).add(task)
            album_hint = str(target).strip() if str(target).strip().isdigit() else ""
            self.progress.set_active(user_id, task, album_hint)
        try:
            async for message in self.runner.run_download(
                event, target, output_format, chapter_expr
            ):
                yield message
        except asyncio.CancelledError:
            yield self.messaging.plain(event, "⏹️ 当前下载已停止")
        finally:
            tasks = self.state.download_tasks.get(user_id)
            if tasks is not None:
                tasks.discard(task)
                if not tasks:
                    self.state.download_tasks.pop(user_id, None)
            self.progress.clear(user_id, task)

    async def stop(self, event: AstrMessageEvent):
        """停止当前用户正在进行的下载"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        user_id = str(event.get_sender_id())
        tasks = [
            task for task in self.state.download_tasks.get(user_id, set())
            if not task.done()
        ]
        if not tasks:
            yield self.messaging.plain(event, "当前没有正在进行的下载")
            return
        for task in tasks:
            task.cancel()
        yield self.messaging.plain(event, f"⏹️ 已请求停止 {len(tasks)} 个下载任务")

    async def now(self, event: AstrMessageEvent):
        """查看当前用户正在进行的下载进度"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        state = self.progress.get_active(event.get_sender_id())
        if not state:
            yield self.messaging.plain(event, "当前还没有下载")
            return

        album_id = str(state.get("album_id") or "")
        done = max(0, int(state.get("done") or 0))
        total = max(0, int(state.get("total") or 0))
        prog = ProgressDownloader._progress.get(album_id) if album_id else None
        if prog:
            done = max(done, int(prog.get("done") or 0))
            total = max(total, int(prog.get("total") or 0))

        if not album_id:
            yield self.messaging.plain(event, "当前下载：正在识别漫画，请稍候")
            return
        if total > 0:
            done = min(done, total)
            yield self.messaging.plain(
                event,
                f"当前下载 JM{album_id}：已完成 {done}/{total} 页，"
                f"未完成 {total - done} 页",
            )
        else:
            yield self.messaging.plain(
                event,
                f"当前下载 JM{album_id}：已完成 {done} 页，总页数统计中",
            )

    async def download_zip(self, event: AstrMessageEvent):
        """下载漫画/章节并发送 ZIP，用法：/jmzip <id> [-c 章节]"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield self.messaging.plain(event,
                "用法：/jmzip <漫画id|章节id> [-c 章节|章节:起页-止页]\n"
                "示例：/jmzip 123456\n"
                "      /jmzip 123456 -c 31-40\n"
                "      /jmzip 123456 -c 5:201-\n"
                "      /jmzip 123,456\n"
                f"说明：多话按后台配置合并 ZIP（当前最多 {self.settings.zip_group_size} 话、"
                f"{self.settings.zip_page_hint()}）；单话目标发一个 ZIP"
            )
            return

        try:
            target, chapter_expr = self.selection.parse_download_args(args)
        except ValueError as e:
            yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jmzip <漫画id|章节id> [-c 章节|章节:起页-止页]")
            return
        if not target:
            yield self.messaging.plain(event, "用法：/jmzip <漫画id|章节id> [-c 章节|章节:起页-止页]")
            return
        async for message in self.consume(
            event, target, "zip", chapter_expr
        ):
            yield message

"""PDF/ZIP 下载、停止与进度查询。"""

import asyncio

from astrbot.api.event import AstrMessageEvent

from ..core.downloader import ProgressDownloader


class DownloadHandler:
    def __init__(self, settings, access, selection, runner, state, progress, messaging):
        self.settings = settings
        self.access = access
        self.selection = selection
        self.runner = runner
        self.state = state
        self.progress = progress
        self.messaging = messaging

    async def download(self, event: AstrMessageEvent):
        """下载漫画/章节并发送 PDF，用法：/jm <id> [-c 章节]"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        args = event.message_str.split()

        # 不带参数或 help，输出帮助
        if len(args) < 2 or args[1].lower() == 'help':
            help_text = (
                "📖 漫画站插件 帮助 v0.7.8\n"
                "━━━━━━━━━━━━━━━\n"
                "🔍 搜索：\n"
                "  /jms 关键词 [页码] [排序]   全站搜索\n"
                "  /jma 作者名 [页码] [排序]   按作者搜索\n"
                "  /jmt 标签名 [页码] [排序]   按标签搜索\n"
                "  /jmr 角色名 [页码] [排序]   按角色搜索\n"
                "  /jmw 原作名 [页码] [排序]   按原作搜索\n"
                "━━━━━━━━━━━━━━━\n"
                "📖 详情：\n"
                "  /jmv <漫画id> [选项]       查详情(含话列表/标签/人物/作品)\n"
                "    选项：-author -work -role -tag -pic(不发封面)\n"
                "  /jmlist <漫画id> [页码]    查看全部话（纯文本，不发封面）\n"
                "  /jmc <漫画id> [页码]        看评论(翻页/回复/剧透，超页自动提示)\n"
                "━━━━━━━━━━━━━━━\n"
                "⬇️ 下载：\n"
                "  /jm <漫画id>                下载整本，PDF按后台页数上限自动分册\n"
                "  /jm <章节id>                自动识别为单话，发一个PDF\n"
                "  /jm <章节id> -c 1:201-      章节ID页段续传（话序号固定写1）\n"
                "  /jm id1,id2,id3             批量下载，支持漫画/章节ID混合\n"
                "  /jm <id> -c 31              只下第31话\n"
                "  /jm <id> -c 31-40           下第31到40话（范围倒置会自动纠正并提示）\n"
                "  /jm <id> -c 31,35,40        下指定话\n"
                "  /jm <id> -c 5:201-          从第5话第201页继续（页段续传）\n"
                "  /jm <id> -c 5:201-400       只下第5话第201-400页\n"
                "  /jmzip <漫画id>             下载整本，按后台话数自动合并ZIP\n"
                "  /jmzip <章节id>             自动识别为单话，发一个ZIP\n"
                "  /jmzip id1,id2,id3          批量下载，支持漫画/章节ID混合\n"
                "  /jmzip <id> -c 31-40        下第31到40话\n"
                "  /jmzip <id> -c 31,35,40     下指定话\n"
                "  /jmphoto <id>               等同 /jm <id>（兼容旧命令）\n"
                "  /jmnow                      查看自己当前下载进度\n"
                "  /jmstop                     停止当前用户正在进行的下载\n"
                "━━━━━━━━━━━━━━━\n"
                "⭐ 账号（仅号主）：\n"
                "  /jmcheckin                  每日签到领金币经验\n"
                "  /jmme                       用户信息面板(签到/收藏/进度)\n"
                "  /jmfav                      收藏夹文件夹列表\n"
                "  /jmfav <文件夹id>           查看指定收藏夹\n"
                "  /jmfav add <漫画id>         添加收藏\n"
                "  /jmfav del <漫画id>         移除收藏\n"
                "  /jmfav export [文件夹id]    导出收藏夹CSV（30秒内再输一次确认）\n"
                "━━━━━━━━━━━━━━━\n"
                "🔔 订阅：\n"
                "  /jmsub <漫画id>             订阅漫画更新\n"
                "  /jmsub list                 查看当前会话订阅\n"
                "  /jmsub del <漫画id>         取消订阅\n"
                "━━━━━━━━━━━━━━━\n"
                "📊 其他：\n"
                "  /jmwhoami                   查看自己的ID（配置号主用，所有人可用）\n"
                "  /jmrank day/week/month [页码]  排行榜\n"
                "  /jmhis [页码]               下载历史（每页{per_page}条）\n"
                "    用法：/jmhis clear 清空历史 | /jmhis -all [页码] 查看全部成员(管理员，每用户每页10条)\n"
                "  /jmdomain                   查看可用域名\n"
                "  /jmupdate                   查看更新日志\n"
                "  /jmrand [关键词]            随机抽一个\n"
                "  /jmnext                     翻到最近一次列表的下一页\n"
                "    也可在 {page_turn_timeout} 秒内直接发送“下一页/下页”\n"
                "━━━━━━━━━━━━━━━\n"
                "📝 排序参数：mr(最新) mv(最多浏览) mp(最多页数) tf(最多收藏) tr(评分最高) md(最多评论)\n"
                "💡 搜索小提示：建议只打前几个关键词，打全容易因简繁差异漏结果\n"
                "💾 下载缓存：JM{{漫画id}}/P{{章节id}}_第N话/图片，标题存入 metadata.json，默认保留 {cache_days} 天\n"
                "📎 打包信息：ZIP 附漫画信息.txt；PDF 写入标题属性，每册都加漫画信息页\n"
                "📄 PDF：每 {pdf_page_limit} 页一册（整话优先，单话超限按页切分）\n"
                "🗜️ ZIP：最多 {zip_group_size} 话/册，{zip_page_text}\n"
                "⚙️ 当前客户端：{client_type}（插件配置可切换 api/html）\n"
                "📦 需 jmcomic 2.7.9+（requirements 会自动安装/升级）\n"
                "⏳ 文件限额：每日{daily_limit_text}、每小时{hourly_limit_text}（0=不限）".format(
                    client_type=self.settings.client_type.upper(),
                    per_page=self.settings.per_page,
                    page_turn_timeout=self.settings.page_turn_timeout,
                    cache_days=self.settings.cache_days,
                    pdf_page_limit=self.settings.pdf_page_limit,
                    zip_group_size=self.settings.zip_group_size,
                    zip_page_text=self.settings.zip_page_hint(),
                    daily_limit_text=self.quota.limit_text(self.settings.daily_file_limit),
                    hourly_limit_text=self.quota.limit_text(self.settings.hourly_file_limit),
                )
            )
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

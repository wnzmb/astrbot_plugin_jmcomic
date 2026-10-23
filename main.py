import os
import asyncio
import zipfile
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

from .core.client import JmClientManager
from .core.config import PluginSettings
from .core.state import PluginState
from .handlers.account import AccountHandler
from .handlers.album import AlbumHandler
from .handlers.download import DownloadHandler
from .handlers.library import LibraryHandler
from .handlers.navigation import NavigationHandler
from .handlers.search import SearchHandler
from .handlers.system import SystemHandler
from .services.access import AccessService
from .services.cache import CacheService
from .services.catalog import CatalogService
from .services.checkin import CheckinService
from .services.download import DownloadService
from .services.download_runner import DownloadRunner
from .services.favorites import FavoritesService
from .services.messaging import MessagingService
from .services.packaging import PackagingService
from .services.pagination import PageSessionService
from .services.history import HistoryService
from .services.progress import ActiveDownloadService
from .services.quota import FileQuotaService
from .services.search import SearchService
from .services.selection import SelectionService
from .services.subscriptions import SubscriptionService
from .services.text import TextService

@register("astrbot_plugin_jmcomic", "wnzmb", "漫画下载插件", "0.7.8")
class JmComicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict[str, Any] | None = None):
        super().__init__(context)
        self.config = config or {}

        # 配置解析与路径/option 构建都在 core.config，这里只做组装。
        # 插件目录由 main.py 传入，避免 core 层用 __file__ 推算导致 data_dir 错两级。
        self.settings = PluginSettings.from_config(
            self.config, Path(os.path.dirname(__file__))
        )
        self.settings.prepare_directories()

        settings = self.settings
        # 兼容层：现有命令实现仍读取插件实例上的这些属性，
        # 后续按批次迁移到 service 后逐项删除。
        self.data_dir = settings.data_dir
        self.download_base_dir = settings.download_base_dir
        self.zip_base_dir = settings.zip_base_dir
        self.send_state_file = settings.send_state_file
        self.history_file = settings.history_file
        self.checkin_file = settings.checkin_file
        self.subscription_file = settings.subscription_file
        self.per_page = settings.per_page
        self.allow_group = settings.allow_group
        self.enabled_groups = settings.enabled_groups
        self.numeric_search_as_album = settings.numeric_search_as_album
        self.subscribe_interval = settings.subscribe_interval
        self.convert_enabled = settings.convert_enabled
        self.convert_title = settings.convert_title
        self.convert_tags = settings.convert_tags
        self.convert_author = settings.convert_author
        self.convert_comment = settings.convert_comment
        self.sensitive_filter = settings.sensitive_filter
        self.tease_enabled = settings.tease_enabled
        self.cache_days = settings.cache_days
        self.comment_count = settings.comment_count
        self.history_max = settings.history_max
        self.dual_search = settings.dual_search
        self.owner_ids = settings.owner_ids
        self.daily_file_limit = settings.daily_file_limit
        self.hourly_file_limit = settings.hourly_file_limit
        self.send_interval = settings.send_interval
        self.zip_group_size = settings.zip_group_size
        self.pdf_page_limit = settings.pdf_page_limit
        self.zip_page_limit = settings.zip_page_limit
        self.page_turn_timeout = settings.page_turn_timeout
        self.use_proxy = settings.use_proxy
        self.proxy_url = settings.proxy_url
        self.client_type = settings.client_type
        self.custom_sensitive_map = settings.custom_sensitive_map
        self.option = settings.option
        self.order_map = settings.order_map

        self.state = PluginState()
        # 登录态客户端缓存：避免每次命令都重新登录
        self.client = JmClientManager(self.settings)
        self.access = AccessService(self.settings)
        self.pagination = PageSessionService(self.settings, self.state)
        self.cache = CacheService(self.settings)
        self.text = TextService(self.settings)
        self.catalog = CatalogService(self.settings, self.cache, self.text)
        self.messaging = MessagingService(self.settings, self.text)
        self.quota = FileQuotaService(self.settings)
        self.history = HistoryService(self.settings)
        self.checkin = CheckinService(self.settings)
        self.favorites = FavoritesService(
            self.settings, self.client, self.text, self.messaging, self.quota
        )
        self.subscriptions = SubscriptionService(
            self.settings, self.client, self.text, self.catalog, self.state, self.context
        )
        self.progress = ActiveDownloadService(self.state)
        self.selection = SelectionService(self.settings, self.cache, self.state)
        self.packaging = PackagingService(self.settings, self.catalog)
        self.search = SearchService(
            self.settings, self.client, self.text, self.messaging, self.pagination
        )
        self.download = DownloadService(self.settings, self.cache, self.messaging)
        self.runner = DownloadRunner(
            self.settings, self.client, self.cache, self.catalog, self.messaging,
            self.quota, self.history, self.progress, self.selection,
            self.download, self.packaging,
        )
        self.account = AccountHandler(
            self.settings, self.access, self.client, self.checkin, self.messaging,
        )
        self.album = AlbumHandler(
            self.settings, self.access, self.client, self.catalog,
            self.pagination, self.messaging, self.text, self.selection,
        )
        self.download_handler = DownloadHandler(
            self.settings, self.access, self.selection, self.runner,
            self.state, self.progress, self.messaging,
        )
        self.search_handler = SearchHandler(
            self.settings, self.access, self.client, self.search,
            self.text, self.messaging, self.album,
        )
        self.library = LibraryHandler(
            self.settings, self.access, self.client, self.history,
            self.pagination, self.messaging, self.text, self.catalog,
            self.selection, self.state,
        )
        self.system = SystemHandler(
            self.settings, self.access, self.client, self.messaging,
        )
        self.navigation = NavigationHandler(
            self.access, self.pagination, self.messaging,
            {
                "jm_chapter_list": self.album.chapter_list,
                "jm_history": self.library.history,
                "jm_comments": self.library.comments,
                "jm_ranking": self.library.ranking,
                "jm_search": self.search_handler.site,
                "jm_search_author": self.search_handler.author,
                "jm_search_tag": self.search_handler.tag,
                "jm_search_actor": self.search_handler.actor,
                "jm_search_work": self.search_handler.work,
            },
        )

        logger.info(
            f"JMComic 插件初始化完成 v0.7.8，客户端: {self.client_type.upper()}，"
            f"镜像站: {settings.custom_domains}，代理: {'已启用' if self.use_proxy else '直连'}"
        )

    # ========== 工具方法 ==========

    def _zip_folder(self, folder_path: Path, zip_path: Path):
        """打包文件夹为ZIP"""
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(folder_path):
                for file in files:
                    file_fullpath = Path(root) / file
                    arcname = file_fullpath.relative_to(folder_path)
                    zipf.write(file_fullpath, arcname)
        return zip_path

    # ========== /jmv 查详情 ==========
    @filter.command("jmv")
    async def jm_view(self, event: AstrMessageEvent):
        """查看漫画详情，用法：/jmv <id> [选项]"""
        async for message in self.album.view(event):
            yield message

    # ========== /jmlist 话列表 ==========
    @filter.command("jmlist")
    async def jm_chapter_list(self, event: AstrMessageEvent):
        """查看漫画话列表，用法：/jmlist <漫画id> [页码]"""
        async for message in self.album.chapter_list(event):
            yield message

    # ========== /jm 下载 PDF（/jmphoto 为兼容别名） ==========
    @filter.command("jm", alias={"jmphoto"})
    async def jm_download(self, event: AstrMessageEvent):
        """下载漫画/章节并发送 PDF，用法：/jm <id> [-c 章节]"""
        async for message in self.download_handler.download(event):
            yield message

    # ========== /jmstop 停止下载 ==========
    @filter.command("jmstop")
    async def jm_stop(self, event: AstrMessageEvent):
        """停止当前用户正在进行的下载"""
        async for message in self.download_handler.stop(event):
            yield message

    # ========== /jmnow 当前下载进度 ==========
    @filter.command("jmnow")
    async def jm_now(self, event: AstrMessageEvent):
        """查看当前用户正在进行的下载进度"""
        async for message in self.download_handler.now(event):
            yield message

    # ========== /jmnext 翻页 ==========
    @filter.command("jmnext")
    async def jm_next(self, event: AstrMessageEvent):
        """翻到最近一次列表的下一页"""
        async for message in self.navigation.next(event):
            yield message

    @filter.regex(r"^(?:下一页|下页)$")
    async def jm_next_keyword(self, event: AstrMessageEvent):
        """在有效期内发送“下一页/下页”自动翻页；无会话时静默。"""
        async for message in self.navigation.next_keyword(event):
            yield message

    # ========== /jmrand 随机漫画 ==========
    @filter.command("jmrand")
    async def jm_random(self, event: AstrMessageEvent):
        """随机抽一个漫画，用法：/jmrand [标签名]"""
        async for message in self.search_handler.random(event):
            yield message

    # ========== /jms 全站搜索 ==========
    @filter.command("jms")
    async def jm_search(self, event: AstrMessageEvent):
        """全站搜索，用法：/jms <关键词> [页码] [排序]"""
        async for message in self.search_handler.site(event):
            yield message

    # ========== /jma 按作者搜索 ==========
    @filter.command("jma")
    async def jm_search_author(self, event: AstrMessageEvent):
        """按作者搜索，用法：/jma <作者名> [页码] [排序]"""
        async for message in self.search_handler.author(event):
            yield message

    # ========== /jmt 按标签搜索 ==========
    @filter.command("jmt")
    async def jm_search_tag(self, event: AstrMessageEvent):
        """按标签搜索，用法：/jmt <标签名> [页码] [排序]"""
        async for message in self.search_handler.tag(event):
            yield message

    # ========== /jmr 按角色搜索 ==========
    @filter.command("jmr")
    async def jm_search_actor(self, event: AstrMessageEvent):
        """按角色/演员搜索，用法：/jmr <角色名> [页码] [排序]"""
        async for message in self.search_handler.actor(event):
            yield message

    # ========== /jmw 按原作搜索 ==========
    @filter.command("jmw")
    async def jm_search_work(self, event: AstrMessageEvent):
        """按原作搜索，用法：/jmw <原作名> [页码] [排序]"""
        async for message in self.search_handler.work(event):
            yield message

    # ========== /jmzip 下载 ZIP ==========
    @filter.command("jmzip")
    async def jm_download_zip(self, event: AstrMessageEvent):
        """下载漫画/章节并发送 ZIP，用法：/jmzip <id> [-c 章节]"""
        async for message in self.download_handler.download_zip(event):
            yield message

    # ========== /jmcheckin 每日签到 ==========
    @filter.command("jmcheckin")
    async def jm_checkin(self, event: AstrMessageEvent):
        """每日签到领金币经验，用法：/jmcheckin"""
        async for message in self.account.daily(event):
            yield message

    # ========== /jmme 用户面板 ==========
    @filter.command("jmme")
    async def jm_user_info(self, event: AstrMessageEvent):
        """查看用户信息面板，用法：/jmme"""
        async for message in self.account.user_info(event):
            yield message

    # ========== /jmrank 排行榜 ==========
    @filter.command("jmrank")
    async def jm_ranking(self, event: AstrMessageEvent):
        """排行榜，用法：/jmrank day|week|month [页码]"""
        async for message in self.library.ranking(event):
            yield message

    # ========== /jmhis 下载历史 ==========
    @filter.command("jmhis")
    async def jm_history(self, event: AstrMessageEvent):
        """查看下载历史，用法：/jmhis 或 /jmhis clear 清空历史"""
        async for message in self.library.history(event):
            yield message

    # ========== /jmc 评论查看 ==========
    @filter.command("jmc")
    async def jm_comments(self, event: AstrMessageEvent):
        """查看评论，用法：/jmc <id> [页码]"""
        async for message in self.library.comments(event):
            yield message

    # ========== /jmwhoami 查看自己的ID ==========
    @filter.command("jmwhoami")
    async def jm_whoami(self, event: AstrMessageEvent):
        """查看自己的发送者ID，用于配置 owner_ids"""
        async for message in self.account.whoami(event):
            yield message

    # ========== /jmdomain 查看可用域名 ==========
    @filter.command("jmdomain")
    async def jm_domain(self, event: AstrMessageEvent):
        """查看 API 与 HTML 两套可用域名"""
        async for message in self.system.domain(event):
            yield message

    # ========== /jmupdate 查看更新日志 ==========
    @filter.command("jmupdate")
    async def jm_changelog(self, event: AstrMessageEvent):
        """查看插件更新日志"""
        async for message in self.system.changelog(event):
            yield message

    # ========== /jmsub 订阅更新 ==========

    @filter.command("jmsub")
    async def jm_subscribe(self, event: AstrMessageEvent):
        """订阅漫画更新，用法：
        /jmsub <漫画id> → 订阅/更新基线
        /jmsub list → 查看当前会话订阅
        /jmsub del <漫画id> → 取消订阅
        """
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        args = event.message_str.split()
        umo = event.unified_msg_origin
        data = self.subscriptions.load()
        subs = data.get(umo)
        if not isinstance(subs, dict):
            subs = {}

        action = args[1].lower() if len(args) >= 2 else "list"
        if action in ("list", "ls"):
            if not subs:
                yield self.messaging.plain(event,
                    "当前会话还没有订阅。\n"
                    "用法：/jmsub <漫画id> 订阅更新"
                )
                return
            lines = [f"🔔 当前会话订阅（{len(subs)}）："]
            for album_id, info in subs.items():
                title = self.text.safe_text(
                    (info or {}).get("title", "") or "未知", self.convert_title
                )
                count = len((info or {}).get("photo_ids") or [])
                lines.append(f"  JM{album_id} {title}（{count}话）")
            lines.append("")
            lines.append("💡 /jmsub <漫画id> 更新基线，/jmsub del <漫画id> 取消")
            yield self.messaging.plain(event, "\n".join(lines))
            return

        if action in ("del", "remove", "unsub"):
            if len(args) != 3:
                yield self.messaging.plain(event, "用法：/jmsub del <漫画id>")
                return
            _, album_id = self.selection.normalize_target(args[2])
            if not album_id.isdigit() or int(album_id) <= 0:
                yield self.messaging.plain(event, f"❌ 漫画ID必须是大于 0 的数字：{args[2]}")
                return
            if album_id not in subs:
                yield self.messaging.plain(event, f"ℹ️ 当前会话没有订阅 JM{album_id}")
                return
            subs.pop(album_id, None)
            if subs:
                data[umo] = subs
            else:
                data.pop(umo, None)
            self.subscriptions.save(data)
            yield self.messaging.plain(event, f"✅ 已取消订阅 JM{album_id}")
            return

        if len(args) != 2:
            yield self.messaging.plain(event,
                "用法：\n"
                "  /jmsub <漫画id>        订阅更新\n"
                "  /jmsub list            查看当前会话订阅\n"
                "  /jmsub del <漫画id>    取消订阅"
            )
            return

        prefix, album_id = self.selection.normalize_target(args[1])
        if prefix == "p" or not album_id.isdigit() or int(album_id) <= 0:
            yield self.messaging.plain(event,
                f"❌ 漫画ID必须是大于 0 的数字：{args[1]}\n"
                "章节ID不能订阅，请填所属漫画ID"
            )
            return

        yield self.messaging.plain(event, f"🔔 正在读取 JM{album_id} 的话信息...")
        try:
            client = await asyncio.to_thread(self.client.get_client)
            album = await asyncio.to_thread(client.get_album_detail, album_id)
            if self.catalog.looks_like_chapter_album(album):
                parent_id = await self.catalog.resolve_chapter_album_parent(
                    client, album, album_id
                )
                if parent_id:
                    yield self.messaging.plain(event,
                        f"❌ JM{album_id} 是章节ID，不能订阅。\n"
                        f"所属漫画：JM{parent_id}\n"
                        f"请用 /jmsub {parent_id} 订阅整本更新。"
                    )
                else:
                    yield self.messaging.plain(event,
                        f"❌ JM{album_id} 是章节ID，不能订阅。请改用所属漫画ID。"
                    )
                return

            snapshot = self.subscriptions.snapshot(album)
            if not snapshot["photo_ids"]:
                yield self.messaging.plain(event,
                    f"⚠️ JM{album_id} 没有话列表，暂时无法订阅"
                )
                return

            existed = album_id in subs
            subs[album_id] = snapshot
            data[umo] = subs
            self.subscriptions.save(data)

            title = self.text.safe_text(
                snapshot["title"] or f"JM{album_id}", self.convert_title
            )
            if self.subscribe_interval > 0:
                interval_text = (
                    f"每 {self.subscribe_interval} 秒检查一次"
                    if self.subscribe_interval < 3600
                    else f"每 {self.subscribe_interval // 3600} 小时检查一次"
                )
                hint = f"后台会{interval_text}，发现新话后推送到当前会话。"
            else:
                hint = "后台自动检查已关闭（subscribe_interval=0），当前只保存订阅，不会推送。"
            yield self.messaging.plain(event,
                f"{'🔄 已更新订阅基线' if existed else '✅ 已订阅'}："
                f"《{title}》JM{album_id}\n"
                f"当前 {len(snapshot['photo_ids'])} 话。{hint}"
            )
        except Exception as e:
            yield self.messaging.plain(event, self.client.handle_exception(e, "订阅"))

    @filter.command("jmfav")
    async def jm_favorite(self, event: AstrMessageEvent):
        """收藏夹功能：
        /jmfav → 查看收藏夹文件夹列表
        /jmfav <文件夹id> → 查看文件夹里的内容
        /jmfav add <漫画id> → 把漫画加入收藏夹
        /jmfav del <漫画id> → 从收藏夹移除
        /jmfav export [文件夹id] → 导出收藏夹 CSV（30 秒内二次确认）
        """
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        if not self.access.is_owner(event):
            yield self.messaging.plain(event, self.access.owner_required_text())
            return
        args = event.message_str.split()

        operation = args[1].lower() if len(args) >= 2 else ""
        if operation in ("add", "del"):
            if len(args) != 3:
                yield self.messaging.plain(event,
                    f"用法：/jmfav {operation} <漫画id>\n"
                    "示例：/jmfav add 123456"
                )
                return
            _, album_id = self.selection.normalize_target(args[2])
            if not album_id.isdigit() or int(album_id) <= 0:
                yield self.messaging.plain(event, f"❌ 漫画ID必须是大于 0 的数字：{args[2]}")
                return
        elif operation == "export":
            if len(args) > 3:
                yield self.messaging.plain(event,
                    "用法：/jmfav export [文件夹id]\n"
                    "不带文件夹id时导出“全部”收藏"
                )
                return
            folder_id = "0"
            if len(args) == 3:
                token = args[2]
                if not re.fullmatch(r"\d+", token):
                    yield self.messaging.plain(event, f"❌ 文件夹ID必须是数字：{token}")
                    return
                folder_id = str(int(token))

            # 二次确认：首次输入只提示，30 秒内再次输入相同命令才真正导出
            user_key = (str(event.get_sender_id()), event.unified_msg_origin)
            action_key = f"export:{folder_id}"
            key = (*user_key, action_key)
            now = time.monotonic()
            self.state.pending_exports = {
                k: v for k, v in self.state.pending_exports.items()
                if now - v <= 30
                and not (
                    k[0] == user_key[0]
                    and k[1] == user_key[1]
                    and k[2] != action_key
                )
            }
            command_text = "/jmfav export"
            if len(args) == 3:
                command_text += f" {args[2]}"
            if key not in self.state.pending_exports:
                self.state.pending_exports[key] = now
                yield self.messaging.plain(event,
                    f"⚠️ 即将导出收藏夹 [ID:{folder_id}] 为 CSV 文件。\n"
                    "导出会把账号收藏数据写入文件并发送到当前会话，请确认不是误触。\n"
                    f"请在 30 秒内再次输入完全相同的命令确认：\n{command_text}"
                )
                return
            self.state.pending_exports.pop(key, None)
            async for item in self.favorites.export(event, folder_id):
                yield item
            return
        elif len(args) >= 2:
            if len(args) > 2:
                yield self.messaging.plain(event, "用法：/jmfav [文件夹id]\n文件夹ID只能填一个")
                return
            folder_token = args[1]
            if not re.fullmatch(r"\d+", folder_token):
                yield self.messaging.plain(event,
                    f"❌ 无法识别的收藏夹参数：{folder_token}\n"
                    "用法：/jmfav | /jmfav <文件夹id> | /jmfav add <漫画id> | "
                    "/jmfav del <漫画id> | /jmfav export [文件夹id]"
                )
                return
            if int(folder_token) < 0:
                yield self.messaging.plain(event, "❌ 文件夹ID不能是负数")
                return

        try:
            client = self.client.get_client()

            # 从配置里读登录的用户名
            username = ""
            try:
                for plugin in self.option.plugins.after_init:
                    if plugin.get('plugin') == 'login':
                        username = plugin.get('kwargs', {}).get('username', '')
                        break
            except:
                pass

            if not username:
                yield self.messaging.plain(event, "⚠️ 未配置登录账号，无法使用收藏夹功能")
                return

            # /jmfav add <id> — 添加收藏（仅API客户端支持）
            if len(args) >= 3 and args[1].lower() == 'add':
                if self.client_type != 'api':
                    yield self.messaging.plain(event, "⚠️ 添加收藏仅支持 API 客户端，请在插件配置中将客户端类型改为 api")
                    return
                _, album_id = self.selection.normalize_target(args[2])
                yield self.messaging.plain(event, f"⭐ 正在检查 JM{album_id} 的收藏状态...")
                is_fav, _album = await self.favorites.favorite_state(client, album_id)
                if is_fav:
                    yield self.messaging.plain(event, f"ℹ️ JM{album_id} 已经在收藏夹里了")
                else:
                    await asyncio.to_thread(client.add_favorite_album, album_id, folder_id='0')
                    self.favorites.clear_client_cache(client)
                    yield self.messaging.plain(event, f"✅ 已收藏 JM{album_id}")
                try:
                    yield self.messaging.plain(event, await self.favorites.render_favorites(client, '0'))
                except Exception as list_err:
                    logger.warning(f"刷新收藏夹失败: {list_err}")
                return

            # /jmfav del <id> — 删除收藏（仅API客户端支持，且需jmcomic较新版本）
            if len(args) >= 3 and args[1].lower() == 'del':
                if self.client_type != 'api':
                    yield self.messaging.plain(event, "⚠️ 移除收藏仅支持 API 客户端，请在插件配置中将客户端类型改为 api")
                    return
                if not hasattr(client, 'delete_favorite_album'):
                    yield self.messaging.plain(event, "⚠️ 当前 jmcomic 版本不支持移除收藏，请升级 jmcomic 到最新版本")
                    return
                _, album_id = self.selection.normalize_target(args[2])
                yield self.messaging.plain(event, f"🗑️ 正在检查 JM{album_id} 的收藏状态...")
                is_fav, _album = await self.favorites.favorite_state(client, album_id)
                if not is_fav:
                    yield self.messaging.plain(event, f"ℹ️ JM{album_id} 本来就不在收藏夹里，无需移除")
                else:
                    await asyncio.to_thread(client.delete_favorite_album, album_id, folder_id='0')
                    self.favorites.clear_client_cache(client)
                    yield self.messaging.plain(event, f"✅ 已移除收藏 JM{album_id}")
                try:
                    yield self.messaging.plain(event, await self.favorites.render_favorites(client, '0'))
                except Exception as list_err:
                    logger.warning(f"刷新收藏夹失败: {list_err}")
                return

            # /jmfav <folder_id> — 查看指定文件夹内容
            if len(args) >= 2 and args[1].isdigit():
                folder_id = args[1]
                yield self.messaging.plain(event, f"⭐ 获取收藏夹 [ID:{folder_id}] 的内容...")
                yield self.messaging.plain(event,
                    await self.favorites.render_favorites(
                        client, folder_id, f"⭐ 收藏夹 [ID:{folder_id}]", 20
                    )
                )
                return

            # /jmfav — 列出收藏夹文件夹
            yield self.messaging.plain(event, "⭐ 获取我的收藏夹列表...")
            page = await asyncio.to_thread(client.favorite_folder, 1)

            # 列出所有收藏夹文件夹
            folders = list(page.iter_folder_id_name())
            if not folders:
                # 如果没有文件夹列表，直接显示全部收藏
                yield self.messaging.plain(event,
                    await self.favorites.render_favorites(client, '0', "⭐ 我的收藏", 20)
                )
                return

            # 显示文件夹列表
            folder_lines = []
            for fid, fname in folders:
                fname_cn = self.text.safe_text(fname, self.convert_title)
                folder_lines.append(f"  📁 [ID:{fid}] {fname_cn}")

            reply = "⭐ 我的收藏夹文件夹：\n\n" + "\n".join(folder_lines)
            reply += "\n\n💡 /jmfav <文件夹id> 查看内容，/jmfav add <漫画id> 添加收藏"
            yield self.messaging.plain(event, reply)

        except Exception as e:
            yield self.messaging.plain(event, self.client.handle_exception(e, "收藏夹操作"))

    async def initialize(self):
        """插件激活时启动订阅更新检查。"""
        if self.subscribe_interval > 0 and self.state.subscribe_task is None:
            if self.state.subscription_lock is None:
                self.state.subscription_lock = asyncio.Lock()
            self.state.subscribe_task = asyncio.create_task(self.subscriptions.loop())
            logger.info(f"订阅更新检查已启动，间隔 {self.subscribe_interval} 秒")

    async def terminate(self):
        task = self.state.subscribe_task
        self.state.subscribe_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"停止订阅检查任务时出错: {e}")

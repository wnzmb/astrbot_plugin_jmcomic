import os
import asyncio
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
from .handlers.favorites import FavoritesHandler
from .handlers.library import LibraryHandler
from .handlers.maintenance import MaintenanceHandler
from .handlers.navigation import NavigationHandler
from .handlers.search import SearchHandler
from .handlers.subscribe import SubscribeHandler
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
from .services.maintenance import MaintenanceService
from .services.progress import ActiveDownloadService
from .services.quota import FileQuotaService
from .services.search import SearchService
from .services.selection import SelectionService
from .services.subscriptions import SubscriptionService
from .services.text import TextService

@register("astrbot_plugin_jmcomic", "wnzmb", "漫画下载插件", "0.7.9")
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
        self.download = DownloadService(self.settings, self.client, self.cache, self.messaging)
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
            self.state, self.progress, self.messaging, self.quota,
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
        self.subscribe = SubscribeHandler(
            self.settings, self.access, self.client, self.catalog,
            self.selection, self.subscriptions, self.text, self.messaging,
        )
        self.favorites_handler = FavoritesHandler(
            self.settings, self.access, self.client, self.selection,
            self.favorites, self.text, self.state, self.messaging,
        )
        self.system = SystemHandler(
            self.settings, self.access, self.client, self.messaging,
        )
        self.maintenance = MaintenanceService(self.settings)
        self.maintenance_handler = MaintenanceHandler(
            self.settings, self.access, self.maintenance, self.state, self.messaging,
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
            f"JMComic 插件初始化完成 v0.7.9，客户端: {settings.client_type.upper()}，"
            f"镜像站: {settings.custom_domains}，代理: {'已启用' if settings.use_proxy else '直连'}"
        )

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
        if not self.settings.page_turn_keyword_enabled:
            return
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

    # ========== /jmtest 连通性诊断 ==========
    @filter.command("jmtest")
    async def jm_test(self, event: AstrMessageEvent):
        """诊断配置域名、代理和登录态"""
        async for message in self.system.test(event):
            yield message

    # ========== /jmdisk 磁盘占用 ==========
    @filter.command("jmdisk")
    async def jm_disk(self, event: AstrMessageEvent):
        """查看下载缓存、ZIP/CSV 临时目录占用"""
        async for message in self.maintenance_handler.disk(event):
            yield message

    # ========== /jmclean 清理图片缓存 ==========
    @filter.command("jmclean")
    async def jm_clean(self, event: AstrMessageEvent):
        """清理图片缓存，保留 metadata.json"""
        async for message in self.maintenance_handler.clean(event):
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
        """订阅漫画更新，用法：/jmsub <漫画id> | list | del <漫画id>"""
        async for message in self.subscribe.handle(event):
            yield message

    # ========== /jmfav 收藏夹 ==========
    @filter.command("jmfav")
    async def jm_favorite(self, event: AstrMessageEvent):
        """收藏夹功能：查看/添加/移除/导出"""
        async for message in self.favorites_handler.handle(event):
            yield message

    async def initialize(self):
        """插件激活时启动订阅更新检查。"""
        if self.settings.subscribe_interval > 0 and self.state.subscribe_task is None:
            if self.state.subscription_lock is None:
                self.state.subscription_lock = asyncio.Lock()
            self.state.subscribe_task = asyncio.create_task(self.subscriptions.loop())
            logger.info(f"订阅更新检查已启动，间隔 {self.settings.subscribe_interval} 秒")

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

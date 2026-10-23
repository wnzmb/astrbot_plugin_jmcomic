import os
import asyncio
import zipfile
import math
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

import jmcomic
from jmcomic import JmModuleConfig
from .core.client import JmClientManager
from .core.config import PluginSettings
from .core.state import PluginState
from .core.parsing import parse_positive_page
from .handlers.album import AlbumHandler
from .handlers.download import DownloadHandler
from .services.access import AccessService
from .services.cache import CacheService
from .services.catalog import CatalogService
from .services.checkin import CheckinService
from .services.download import DownloadService
from .services.download_runner import DownloadRunner
from .services.favorites import FavoritesService
from .services.messaging import MessagingService
from .services.packaging import PackagingService
from .services.pagination import PageEventProxy, PageSessionService
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
        self.album = AlbumHandler(
            self.settings, self.access, self.client, self.catalog,
            self.pagination, self.messaging, self.text, self.selection,
        )
        self.download_handler = DownloadHandler(
            self.settings, self.access, self.selection, self.runner,
            self.state, self.progress, self.messaging,
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
    async def _follow_page_session(self, event, session):
        next_command = session.get("next_command", "")
        parts = next_command.strip().split()
        if not parts:
            return
        command = parts[0].lstrip("/").lower()
        handler_name = self.pagination.page_handler_name(command)
        handler = getattr(self, handler_name, None) if handler_name else None
        if handler is None:
            return
        proxy = PageEventProxy(event, next_command)
        async for message in handler(proxy):
            yield message

    @filter.command("jmnext")
    async def jm_next(self, event: AstrMessageEvent):
        """翻到最近一次列表的下一页"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        session = self.pagination.get(event)
        if session is None:
            yield self.messaging.plain(
                event,
                "当前没有可翻页的列表，或已超时。请先使用 /jmlist、/jmhis、"
                "/jmc、/jmrank 或搜索命令。",
            )
            return
        self.pagination.clear(event)
        async for message in self._follow_page_session(event, session):
            yield message

    @filter.regex(r"^(?:下一页|下页)$")
    async def jm_next_keyword(self, event: AstrMessageEvent):
        """在有效期内发送“下一页/下页”自动翻页；无会话时静默。"""
        if not self.access.check_permission(event):
            return
        session = self.pagination.get(event)
        if session is None:
            return
        self.pagination.clear(event)
        async for message in self._follow_page_session(event, session):
            yield message

    # ========== /jmrand 随机漫画 ==========
    @filter.command("jmrand")
    async def jm_random(self, event: AstrMessageEvent):
        """随机抽一个漫画，用法：/jmrand [标签名]
        不填标签就从日榜随机，填了就从指定标签里随机
        """
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        args = event.message_str.split()
        tag_name = " ".join(args[1:]) if len(args) > 1 else None

        if tag_name:
            yield self.messaging.plain(event, f"🎲 正在从「{tag_name}」标签随机抽一个...")
        else:
            yield self.messaging.plain(event, "🎲 正在从日榜随机抽一个漫画...")

        try:
            import random
            client = self.client.get_client()

            if tag_name:
                # 全站搜索，关键词可以是标题/标签/作者
                page = await asyncio.to_thread(client.search_site, tag_name, 1)
                items = list(page.iter_id_title_tag())
                if not items:
                    yield self.messaging.plain(event,
                        f"🎲 没有找到与「{tag_name}」相关的漫画，请换个关键词。\n"
                        "特殊符号（如 #）可能无法被搜索接口识别。"
                    )
                    return
            else:
                # 日榜可能暂时为空，依次尝试日/周/月榜的前3页增加随机范围
                items = []
                for method_name in ("day_ranking", "week_ranking", "month_ranking"):
                    method = getattr(client, method_name, None)
                    if method is None:
                        continue
                    for page_num in range(1, 4):
                        try:
                            page = await asyncio.to_thread(method, page_num)
                            page_items = list(page.iter_id_title_tag())
                        except Exception:
                            page_items = []
                        if page_items:
                            items.extend(page_items)
                    if items:
                        break
                if not items:
                    yield self.messaging.plain(event,
                        "🎲 暂时没有抽到漫画：日榜/周榜/月榜均未返回结果。\n"
                        "请稍后再试，或使用 /jmrand 关键词 从搜索中随机抽取。"
                    )
                    return

            album_id, title, tags = random.choice(items)
            title = self.text.safe_text(title, self.convert_title)
            tags = [self.text.safe_text(t, self.convert_tags) for t in tags]
            safe_tags = tags[:5]

            # 查一下详情，拿章节数和页数
            album = await asyncio.to_thread(client.get_album_detail, album_id)
            chapters = list(album) if hasattr(album, '__iter__') else []
            page_count = getattr(album, 'page_count', '?')

            tag_str = f"\n🏷️ {', '.join(safe_tags)}" if safe_tags else ""
            yield self.messaging.plain(event,
                f"🎲 抽到了！\n\n"
                f"📖 {title}\n"
                f"🆔 JM{album_id}"
                f"{tag_str}\n"
                f"📄 {len(chapters)}话 / {page_count}页\n\n"
                f"发 /jm {album_id} 下载"
            )

        except Exception as e:
            yield self.messaging.plain(event, self.client.handle_exception(e, "随机抽漫"))

    # ========== /jms 全站搜索 ==========
    @filter.command("jms")
    async def jm_search(self, event: AstrMessageEvent):
        """全站搜索，用法：/jms <关键词> [页码] [排序]"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield self.messaging.plain(event,
                "用法：\n"
                "  /jms <关键词>\n"
                "  /jms <关键词> 2\n"
                "  /jms <关键词> mv   (mv=最多浏览, tf=最多收藏)"
            )
            return

        # 纯数字搜索可选直接按车号看详情（默认关闭，避免改变普通搜索语义）
        if self.numeric_search_as_album and len(args) == 2 and args[1].isdigit():
            async for result in self.jm_view(event):
                yield result
            return

        try:
            keyword, qq_page, order_by = self.search.parse_search_args(args)
        except ValueError as e:
            yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jms <关键词> [页码] [排序]")
            return
        client = self.client.get_client()

        async for result in self.search.render_search_page(
            event, client, client.search_site, keyword, qq_page, order_by, "全站搜索", "jms"
        ):
            yield result

    # ========== /jma 按作者搜索 ==========
    @filter.command("jma")
    async def jm_search_author(self, event: AstrMessageEvent):
        """按作者搜索，用法：/jma <作者名> [页码] [排序]"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield self.messaging.plain(event, "用法：/jma <作者名>")
            return

        try:
            keyword, qq_page, order_by = self.search.parse_search_args(args)
        except ValueError as e:
            yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jma <作者名> [页码] [排序]")
            return
        client = self.client.get_client()

        async for result in self.search.render_search_page(
            event, client, client.search_author, keyword, qq_page, order_by, "作者搜索", "jma"
        ):
            yield result

    # ========== /jmt 按标签搜索 ==========
    @filter.command("jmt")
    async def jm_search_tag(self, event: AstrMessageEvent):
        """按标签搜索，用法：/jmt <标签名> [页码] [排序]"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield self.messaging.plain(event, "用法：/jmt <标签名>")
            return

        try:
            keyword, qq_page, order_by = self.search.parse_search_args(args)
        except ValueError as e:
            yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jmt <标签名> [页码] [排序]")
            return
        client = self.client.get_client()

        async for result in self.search.render_search_page(
            event, client, client.search_tag, keyword, qq_page, order_by, "标签搜索", "jmt"
        ):
            yield result

    # ========== /jmr 按角色搜索 ==========
    @filter.command("jmr")
    async def jm_search_actor(self, event: AstrMessageEvent):
        """按角色/演员搜索，用法：/jmr <角色名> [页码] [排序]"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield self.messaging.plain(event, "用法：/jmr <角色名>")
            return

        try:
            keyword, qq_page, order_by = self.search.parse_search_args(args)
        except ValueError as e:
            yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jmr <角色名> [页码] [排序]")
            return
        client = self.client.get_client()

        async for result in self.search.render_search_page(
            event, client, client.search_actor, keyword, qq_page, order_by, "角色搜索", "jmr"
        ):
            yield result

    # ========== /jmw 按原作搜索 ==========
    @filter.command("jmw")
    async def jm_search_work(self, event: AstrMessageEvent):
        """按原作搜索，用法：/jmw <原作名> [页码] [排序]"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield self.messaging.plain(event, "用法：/jmw <原作名>")
            return

        try:
            keyword, qq_page, order_by = self.search.parse_search_args(args)
        except ValueError as e:
            yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jmw <原作名> [页码] [排序]")
            return
        client = self.client.get_client()

        async for result in self.search.render_search_page(
            event, client, client.search_work, keyword, qq_page, order_by, "原作搜索", "jmw"
        ):
            yield result

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
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        if not self.access.is_owner(event):
            yield self.messaging.plain(event, self.access.owner_required_text())
            return
        try:
            client = self.client.get_client()

            # jmcomic 2.7.5 及以下版本不支持签到接口
            jm_version = getattr(jmcomic, '__version__', '未知')
            if not hasattr(client, 'get_daily') or not hasattr(client, 'daily_checkin'):
                yield self.messaging.plain(event,
                    f"⚠️ 当前 jmcomic 版本（{jm_version}）不支持每日签到功能\n"
                    f"需要 jmcomic >= 2.7.7（推荐升级到 2.7.9），请升级后重启 AstrBot：\n"
                    f"  D:\\Program Files\\AstrBot\\backend\\python\\python.exe -m pip install -U jmcomic"
                )
                return

            # 先获取签到信息
            yield self.messaging.plain(event, "📅 正在获取签到信息...")
            daily = await asyncio.to_thread(client.get_daily)
            info = self.checkin.parse_daily_info(daily)

            # 执行签到
            yield self.messaging.plain(event, "✅ 正在签到...")
            signed_ok = False
            try:
                resp = await asyncio.to_thread(client.daily_checkin)
                if resp.code == 0:
                    result_msg = f"🎉 签到成功！\n{resp.msg}"
                    signed_ok = True
                elif resp.code == 1:
                    result_msg = f"📌 今日已签到\n{resp.msg}"
                    signed_ok = True
                else:
                    result_msg = f"签到结果：{resp.msg}"
            except Exception as checkin_err:
                # jmcomic 2.7.9 的关键词匹配只支持繁体，API返回简体"今天已经签到过了"时会抛异常
                err_str = str(checkin_err)
                if any(kw in err_str for kw in ('已签', '签到过', '已经签', '今日已')):
                    result_msg = f"📌 今日已签到\n{err_str.replace('签到失败：', '')}"
                    signed_ok = True
                else:
                    raise

            # 签到后重新拉取状态：API 客户端的连续天数要从 record 日历反推
            if signed_ok:
                try:
                    daily_after = await asyncio.to_thread(client.get_daily)
                    final_info = self.checkin.parse_daily_info(daily_after, assume_today_signed=True)
                except Exception:
                    final_info = self.checkin.parse_daily_info(daily, assume_today_signed=True)
            else:
                final_info = info

            if final_info.get('signed_count') is None:
                final_info['signed_count'] = info.get('signed_count')
            if final_info.get('streak') is None:
                final_info['streak'] = info.get('streak')
            if final_info.get('streak') is None:
                final_info['streak'] = self.checkin.local_streak(bool(final_info.get('today_signed')))

            if signed_ok:
                streak_to_save = final_info.get('streak')
                if streak_to_save is None:
                    streak_to_save = 1
                self.checkin.save_state(streak_to_save, final_info.get('signed_count'))

            streak_text = f"{final_info['streak']} 天" if final_info.get('streak') is not None else "获取失败"
            reply = f"📅 每日签到\n\n{result_msg}\n\n"
            reply += f"🔥 连续签到：{streak_text}\n"
            if final_info.get('signed_count') is not None:
                reply += f"📆 本月已签：{final_info['signed_count']} 天"
            if final_info.get('today_signed'):
                reply += "\n\n✅ 今日已完成签到"

            yield self.messaging.plain(event, reply)

        except Exception as e:
            yield self.messaging.plain(event, self.client.handle_exception(e, "签到"))

    # ========== /jmme 用户面板 ==========
    @filter.command("jmme")
    async def jm_user_info(self, event: AstrMessageEvent):
        """查看用户信息面板，用法：/jmme"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        if not self.access.is_owner(event):
            yield self.messaging.plain(event, self.access.owner_required_text())
            return
        try:
            client = self.client.get_client()

            # 从配置读取登录用户名
            username = ""
            try:
                for plugin in self.option.plugins.after_init:
                    if plugin.get('plugin') == 'login':
                        username = plugin.get('kwargs', {}).get('username', '')
                        break
            except:
                pass

            if not username:
                yield self.messaging.plain(event, "⚠️ 未配置登录账号，请在插件配置中填写账号密码")
                return

            # 获取签到信息（jmcomic 2.7.5+ 才支持）
            info = {}
            if hasattr(client, 'get_daily'):
                try:
                    daily = await asyncio.to_thread(client.get_daily)
                    info = self.checkin.parse_daily_info(daily)
                except Exception:
                    info = {}

                # 接口日历更新延迟时，用本机签到记录兜底
                state = self.checkin.load_state()
                today_str = datetime.now().strftime('%Y-%m-%d')
                if state.get('date') == today_str:
                    info['today_signed'] = True
                if info.get('streak') is None:
                    info['streak'] = self.checkin.local_streak(bool(info.get('today_signed')))
                if info.get('signed_count') is None and state.get('signed_count') is not None:
                    info['signed_count'] = state.get('signed_count')

            # 获取收藏数（通过收藏夹第一页）
            fav_count = "未知"
            try:
                fav_page = await asyncio.to_thread(client.favorite_folder, 1)
                fav_total = getattr(fav_page, 'total', None)
                if fav_total is not None:
                    fav_count = str(fav_total)
            except Exception:
                pass

            reply = f"👤 用户面板\n\n"
            reply += f"📛 用户名：{username}\n"
            if hasattr(client, 'get_daily'):
                streak = info.get('streak')
                streak_text = f"{streak} 天" if streak is not None else "获取失败"
                reply += f"🔥 连续签到：{streak_text}\n"
                reply += f"📅 今日签到：{'✅ 已签' if info.get('today_signed') else '❌ 未签'}\n"
                if info.get('progress'):
                    reply += f"📈 活动进度：{info['progress']}\n"
            reply += f"⭐ 收藏数量：{fav_count}\n"
            if hasattr(client, 'daily_checkin'):
                reply += f"\n💡 /jmcheckin 立即签到"
            else:
                reply += f"\n💡 签到功能需升级 jmcomic 到最新版本"

            yield self.messaging.plain(event, reply)

        except Exception as e:
            yield self.messaging.plain(event, self.client.handle_exception(e, "获取用户信息"))

    # ========== /jmrank 排行榜 ==========
    @filter.command("jmrank")
    async def jm_ranking(self, event: AstrMessageEvent):
        """排行榜，用法：/jmrank day|week|month [页码]"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        self.pagination.clear(event)
        args = event.message_str.split()
        if len(args) < 2:
            yield self.messaging.plain(event,
                "用法：\n"
                "  /jmrank day [页码]    日榜\n"
                "  /jmrank week [页码]   周榜\n"
                "  /jmrank month [页码]  月榜"
            )
            return

        rank_type = args[1].lower()

        rank_map = {
            'day': ('日榜', 'day_ranking'),
            'week': ('周榜', 'week_ranking'),
            'month': ('月榜', 'month_ranking'),
        }

        if rank_type not in rank_map:
            yield self.messaging.plain(event, "类型只能是 day / week / month")
            return
        if len(args) > 3:
            yield self.messaging.plain(event, "用法：/jmrank day|week|month [页码]\n页码只能填一个")
            return
        try:
            qq_page = parse_positive_page(args[2]) if len(args) >= 3 else 1
        except ValueError as e:
            yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jmrank {rank_type} [页码]")
            return

        rank_name, method_name = rank_map[rank_type]
        yield self.messaging.plain(event, f"🏆 获取{rank_name} 第{qq_page}页...")

        try:
            client = self.client.get_client()
            method = getattr(client, method_name)

            PER_QQ_PAGE = self.per_page
            PER_WEB_PAGE = 80
            QQ_PAGES_PER_WEB_PAGE = PER_WEB_PAGE // PER_QQ_PAGE

            web_page = math.ceil(qq_page / QQ_PAGES_PER_WEB_PAGE)
            offset_in_web_page = (qq_page - 1) % QQ_PAGES_PER_WEB_PAGE * PER_QQ_PAGE

            page = await asyncio.to_thread(method, web_page)
            all_items = list(page.iter_id_title_tag())

            start = offset_in_web_page
            end = start + PER_QQ_PAGE
            page_items = all_items[start:end]

            if not page_items:
                self.pagination.clear(event)
                yield self.messaging.plain(event, "没有更多了")
                return

            results = []
            for i, (aid, title, tags) in enumerate(page_items):
                global_index = (qq_page - 1) * PER_QQ_PAGE + i + 1
                title = self.text.safe_text(title, self.convert_title)
                tags = [self.text.safe_text(t, self.convert_tags) for t in tags]
                safe_tags = tags[:3]
                tag_str = f" [{', '.join(safe_tags)}]" if safe_tags else ""
                results.append(f"{global_index}. [JM{aid}] {title}{tag_str}")

            reply = f"🏆 {rank_name}（第{qq_page}页）\n\n" + "\n".join(results)
            reply += f"\n\n下一页：/jmrank {rank_type} {qq_page + 1}"
            self.pagination.remember(
                event, f"/jmrank {rank_type} {qq_page + 1}", qq_page, None
            )
            yield self.messaging.plain(event, reply)

        except Exception as e:
            yield self.messaging.plain(event, self.client.handle_exception(e, "获取排行榜"))

    # ========== /jmhis 下载历史 ==========
    @filter.command("jmhis")
    async def jm_history(self, event: AstrMessageEvent):
        """查看下载历史，用法：/jmhis 或 /jmhis clear 清空历史"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        self.pagination.clear(event)
        args = event.message_str.split()
        user_id = event.get_sender_id()

        if len(args) >= 2 and args[1].lower() == "clear":
            data = self.history.load()
            if user_id in data:
                del data[user_id]
            self.history.save(data)
            yield self.messaging.plain(event, "🧹 你的下载历史已清空")
            return

        # /jmhis -all：管理员查看所有成员的历史，页码与 -all 可任意顺序
        show_all = any(token.lower() in ("-all", "all") for token in args[1:])
        page_num = 1
        page_seen = False
        if show_all:
            for token in args[1:]:
                if token.lower() in ("-all", "all"):
                    continue
                if page_seen:
                    yield self.messaging.plain(event, "用法：/jmhis [页码] -all\n页码只能填一个")
                    return
                try:
                    page_num = parse_positive_page(token)
                    page_seen = True
                except ValueError as e:
                    yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jmhis [页码] -all")
                    return

            is_admin = event.is_admin() if hasattr(event, 'is_admin') else getattr(event, 'role', '') == 'admin'
            if not is_admin:
                yield self.messaging.plain(event, "⚠️ 只有管理员可以使用 /jmhis -all")
                return
            data = self.history.load()
            if not data:
                yield self.messaging.plain(event, "还没有任何下载记录")
                return
            page_size = 10
            active_users = [(uid, items) for uid, items in data.items() if items]
            if not active_users:
                yield self.messaging.plain(event, "还没有任何下载记录")
                return
            total_users = len(active_users)
            total_records = sum(len(items) for _uid, items in active_users)
            total_pages = max(
                1,
                max(math.ceil(len(items) / page_size) for _uid, items in active_users),
            )
            if page_num > total_pages:
                yield self.messaging.plain(event,
                    f"⚠️ 全部成员历史共 {total_records} 条，只有 {total_pages} 页，"
                    f"已到最后一页（/jmhis -all {total_pages}）"
                )
                return

            lines = []
            for uid, items in active_users:
                start = (page_num - 1) * page_size
                page_items = items[start:start + page_size]
                lines.append(
                    f"👤 用户 {uid}（共{len(items)}条，本页{len(page_items)}条）"
                )
                if not page_items:
                    lines.append("  （本页无记录）")
                for offset, item in enumerate(page_items, 1):
                    title = self.text.safe_text(item.get('title', ''), self.convert_title)
                    lines.append(
                        f"  {start + offset}. [JM{item.get('id', '')}] {title[:30]}  "
                        f"({item.get('size_mb', '?')}MB)  {item.get('time', '')}"
                    )
                lines.append("")
            reply = (
                f"📜 全部成员下载历史（{total_users}人，共{total_records}条，"
                f"第{page_num}/{total_pages}页）\n\n"
                + "\n".join(lines)
            ).rstrip()
            nav = []
            if page_num > 1:
                nav.append(f"上一页：/jmhis -all {page_num - 1}")
            if page_num < total_pages:
                nav.append(f"下一页：/jmhis -all {page_num + 1}")
            if nav:
                reply += "\n\n" + "\n".join(nav)
            if page_num < total_pages:
                self.pagination.remember(
                    event, f"/jmhis -all {page_num + 1}", page_num, total_pages
                )
            yield self.messaging.plain(event, reply)
            return

        if len(args) > 2:
            yield self.messaging.plain(event, "用法：/jmhis [页码]\n页码只能填一个")
            return
        try:
            page_num = parse_positive_page(args[1]) if len(args) >= 2 else 1
        except ValueError as e:
            yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jmhis [页码]")
            return

        history = self.history.get_user(user_id)
        if not history:
            yield self.messaging.plain(event, "还没有下载记录")
            return

        page_size = self.per_page
        total_pages = max(1, math.ceil(len(history) / page_size))
        if page_num > total_pages:
            yield self.messaging.plain(event,
                f"⚠️ 下载历史共 {len(history)} 条，只有 {total_pages} 页，已到最后一页\n"
                f"🔙 回到最后一页：/jmhis {total_pages}"
            )
            return

        start = (page_num - 1) * page_size
        page_items = history[start:start + page_size]
        results = []
        for offset, item in enumerate(page_items, 1):
            title = self.text.safe_text(item.get('title', ''), self.convert_title)
            results.append(
                f"{start + offset}. [JM{item.get('id', '')}] {title[:30]}  "
                f"({item.get('size_mb', '?')}MB)  {item.get('time', '')}"
            )

        reply = (
            f"📜 下载历史（第{page_num}/{total_pages}页，共{len(history)}条）：\n\n"
            + "\n".join(results)
        )
        if page_num < total_pages:
            reply += f"\n\n下一页：/jmhis {page_num + 1}"
        if page_num > 1:
            reply += f"\n上一页：/jmhis {page_num - 1}"
        reply += "\n\n/jmhis clear 清空历史"
        if page_num < total_pages:
            self.pagination.remember(
                event, f"/jmhis {page_num + 1}", page_num, total_pages
            )
        yield self.messaging.plain(event, reply)

    # ========== /jmc 评论查看 ==========
    @filter.command("jmc")
    async def jm_comments(self, event: AstrMessageEvent):
        """查看评论，用法：/jmc <id> [页码]
        支持翻页，展示回复楼、剧透标记、点赞数
        """
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        self.pagination.clear(event)
        args = event.message_str.split()
        if len(args) < 2:
            yield self.messaging.plain(event, "用法：/jmc <漫画id> [页码]\n示例：/jmc 123456 或 /jmc 123456 2")
            return

        prefix, album_id = self.selection.normalize_target(args[1])
        if prefix == "p" or not album_id.isdigit():
            yield self.messaging.plain(event,
                f"❌ 漫画ID必须是数字：{args[1]}\n"
                "章节ID不能查评论，请用 /jm <章节id> 下载"
            )
            return
        if len(args) > 3:
            yield self.messaging.plain(event, "用法：/jmc <漫画id> [页码]\n页码只能填一个")
            return
        try:
            page_num = parse_positive_page(args[2]) if len(args) >= 3 else 1
        except ValueError as e:
            yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jmc {album_id} [页码]")
            return

        # HTML 客户端不支持评论翻页，最多10条
        if page_num > 1 and self.client_type != 'api':
            yield self.messaging.plain(event, "⚠️ 评论翻页仅支持 API 客户端，请在插件配置中将客户端类型改为 api")
            return

        yield self.messaging.plain(event, f"💬 获取 JM{album_id} 的评论（第{page_num}页）...")

        try:
            client = self.client.get_client()

            # 先校验漫画是否存在，避免无效ID被上游当成“0页评论”
            album = await asyncio.to_thread(client.get_album_detail, album_id)
            if self.catalog.looks_like_chapter_album(album):
                parent_id = await self.catalog.resolve_chapter_album_parent(client, album, album_id)
                parent_line = (
                    f"该话所属漫画：JM{parent_id}，可用 /jmv {parent_id} 查看。\n"
                    if parent_id else ""
                )
                yield self.messaging.plain(event,
                    f"❌ JM{album_id} 看起来是章节ID，不是漫画ID。\n"
                    f"{parent_line}"
                    f"章节ID不能查评论，请用 /jm {album_id} 下载。"
                )
                return

            # 超页检测：如果已知总页数且超过，直接提示（避免API返回重复内容）
            comment_cache_key = f"comment_pages_{album_id}"
            known_max_page = self.state.comment_page_cache.get(comment_cache_key)
            if known_max_page and page_num > known_max_page:
                yield self.messaging.plain(event, f"⚠️ JM{album_id} 评论共 {known_max_page} 页，已到最后一页")
                return

            page = await asyncio.to_thread(client.album_pagination, album_id, page=page_num)

            # 先拿到总页数并缓存（API 的 page_count 由 total 计算，最多500页）
            total = getattr(page, 'total', None)
            page_count = getattr(page, 'page_count', None)
            if page_count:
                self.state.comment_page_cache[comment_cache_key] = page_count

            # 0 条评论不是“超页”，优先显示自然文案
            if page_count == 0:
                yield self.messaging.plain(event, "还没有评论")
                return

            # 请求页码超过总页数时，上游会重复返回第一页内容，这里直接拦截
            if page_count is not None and page_num > page_count:
                yield self.messaging.plain(event,
                    f"⚠️ JM{album_id} 评论共 {page_count} 页，已到最后一页\n"
                    f"🔙 回到第1页：/jmc {album_id} 1"
                )
                return

            comments = list(page)
            if not comments:
                if page_num > 1:
                    yield self.messaging.plain(event,
                        f"⚠️ 第{page_num}页没有评论，已经到最后一页了\n"
                        f"🔙 上一页：/jmc {album_id} {page_num - 1}"
                    )
                else:
                    yield self.messaging.plain(event, "还没有评论")
                return

            id_to_name = {}

            def collect_names(items):
                for item in items:
                    name = getattr(item, 'nickname', None) or getattr(item, 'username', None) or '匿名'
                    cid = getattr(item, 'comment_id', None)
                    if cid is not None:
                        id_to_name[str(cid)] = name
                    collect_names(getattr(item, 'replies', []) or [])

            collect_names(comments)

            def format_comment(comment, indent=0, is_reply=False):
                """格式化单条评论，回复用 ↳ 单独标记并显示回复对象"""
                prefix = "  " * indent
                nickname = getattr(comment, 'nickname', None) or getattr(comment, 'username', None) or '匿名'
                nickname = self.text.safe_text(nickname, False)
                raw_content = getattr(comment, 'content', '') or ''
                content = self.text.safe_text(raw_content.replace('\n', ' ').strip()[:120], self.convert_comment)

                reply_to = ""
                parent_id = getattr(comment, 'parent_comment_id', None)
                if is_reply:
                    parent_name = id_to_name.get(str(parent_id)) if parent_id is not None else None
                    if parent_name:
                        parent_name = self.text.safe_text(parent_name, False)
                    reply_to = f" 回复 @{parent_name}" if parent_name else " 回复"

                spoiler = " ⚠️剧透" if getattr(comment, 'is_spoiler', False) else ""
                likes = getattr(comment, 'likes', None)
                likes_str = f" 👍{likes}" if likes is not None else ""
                created = getattr(comment, 'created_at', '')
                time_str = f" ({created})" if created else ""

                arrow = "↳ " if is_reply else ""
                line = f"{prefix}{arrow}{nickname}{reply_to}{spoiler}{likes_str}{time_str}：{content}"
                lines = [line]

                # 嵌套回复
                for reply in getattr(comment, 'replies', []) or []:
                    lines.extend(format_comment(reply, indent + 1, True))

                return lines

            results = []
            for i, comment in enumerate(comments[:self.comment_count]):
                block = format_comment(comment)
                results.append(f"{i+1}. {block[0]}")
                results.extend(block[1:])

            # 分页信息（total/page_count 已在上方获取并缓存）
            header = f"💬 JM{album_id} 评论（第{page_num}页"
            if total is not None:
                header += f"，共{total}条"
                # 上游API评论接口最多返回5000条，超过会被截断
                if total >= 5000:
                    header += "（上游API上限5000条）"
            if page_count is not None:
                header += f"，共{page_count}页"
            header += "）"

            reply = header + "\n\n" + "\n".join(results)

            # 下一页提示
            if page_count is None or page_num < page_count:
                reply += f"\n\n下一页：/jmc {album_id} {page_num + 1}"
                self.pagination.remember(
                    event,
                    f"/jmc {album_id} {page_num + 1}",
                    page_num,
                    page_count,
                )

            yield self.messaging.plain(event, reply[:3500])

        except Exception as e:
            yield self.messaging.plain(event, self.client.handle_exception(e, "获取评论"))

    # ========== /jmwhoami 查看自己的ID ==========
    @filter.command("jmwhoami")
    async def jm_whoami(self, event: AstrMessageEvent):
        """查看自己的发送者ID，用于配置 owner_ids"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        sender_id = str(event.get_sender_id())
        is_admin = event.is_admin() if hasattr(event, 'is_admin') else getattr(event, 'role', '') == 'admin'
        is_owner = self.access.is_owner(event)
        lines = [
            f"🆔 你的发送者ID：{sender_id}",
            f"👑 AstrBot管理员：{'是' if is_admin else '否'}",
            f"🔑 JM账号号主：{'是' if is_owner else '否'}",
        ]
        if not self.owner_ids:
            lines.append("\n⚠️ 尚未配置 owner_ids，账号类命令暂时不可用。")
            lines.append(f"把上面的ID填入插件配置 owner_ids 即可。")
        yield self.messaging.plain(event, "\n".join(lines))

    # ========== /jmdomain 查看可用域名 ==========
    @filter.command("jmdomain")
    async def jm_domain(self, event: AstrMessageEvent):
        """查看 API 与 HTML 两套可用域名"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        yield self.messaging.plain(event, "🌐 正在获取可用域名...")
        try:
            client = self.client.get_client()
            client_label = "API" if self.client_type == "api" else "HTML"

            api_domains = []
            html_domains = []

            # 当前客户端已知域名
            try:
                current_domains = list(client.get_domain_list())
                if self.client_type == "api":
                    api_domains = current_domains
                else:
                    html_domains = current_domains
            except Exception:
                pass

            # API 域名：优先使用库自动更新后的列表
            if not api_domains:
                api_domains = list(
                    JmModuleConfig.DOMAIN_API_UPDATED_LIST
                    or JmModuleConfig.DOMAIN_API_LIST
                    or []
                )

            # HTML 域名：通过 jmcomic 发布页获取，失败则回退到库缓存
            if not html_domains:
                try:
                    html_domains = list(await asyncio.to_thread(JmModuleConfig.get_html_domain_all))
                except Exception:
                    html_domains = []
                if not html_domains:
                    fallback = JmModuleConfig.DOMAIN_HTML_LIST or JmModuleConfig.DOMAIN_HTML
                    if isinstance(fallback, str):
                        html_domains = [fallback]
                    elif fallback:
                        html_domains = list(fallback)

            domain_str = self.config.get("domain", "")
            custom_domains = [d.strip() for d in domain_str.split(",") if d.strip()]

            reply = f"🌐 JM 域名信息（当前客户端：{client_label}）\n\n"
            reply += "📡 API 域名（接口请求用，浏览器打开无画面）：\n"
            if api_domains:
                for i, d in enumerate(api_domains[:10]):
                    reply += f"  {i+1}. {d}\n"
            else:
                reply += "  暂未获取到\n"

            reply += "\n🌍 HTML 域名（浏览器可直接访问）：\n"
            if html_domains:
                for i, d in enumerate(html_domains[:10]):
                    reply += f"  {i+1}. {d}\n"
            else:
                reply += "  暂未获取到\n"

            if custom_domains:
                reply += f"\n⚙️ 你配置的自定义域名：{', '.join(custom_domains)}\n"
            reply += (
                "\n💡 API 客户端会自动选择最快域名并更新；"
                "HTML 客户端可在配置中填写可用域名\n"
            )

            yield self.messaging.plain(event, reply)
        except Exception as e:
            yield self.messaging.plain(event, self.client.handle_exception(e, "获取域名"))

    # ========== /jmupdate 查看更新日志 ==========
    @filter.command("jmupdate")
    async def jm_changelog(self, event: AstrMessageEvent):
        """查看插件更新日志"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        changelog_path = Path(__file__).parent / "CHANGELOG.md"
        if not changelog_path.exists():
            yield self.messaging.plain(event, "❌ 更新日志文件不存在")
            return
        content = changelog_path.read_text(encoding="utf-8")
        # 只发最新版本：从第一个二级标题开始，到下一个二级标题前结束
        match = re.search(r"^##\s+.*?(?=^##\s+|\Z)", content, re.S | re.M)
        reply = match.group(0).strip() if match else content.strip()
        reply = re.sub(r"\n+---\s*$", "", reply).strip()
        if len(reply) > 1800:
            reply = reply[:1750] + "\n...（已截断）"
        yield self.messaging.plain(event, f"📝 最新更新日志：\n\n{reply}")

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

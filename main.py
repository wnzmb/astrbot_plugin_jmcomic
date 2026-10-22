import os
import io
import csv
import asyncio
import zipfile
import shutil
import math
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
import astrbot.api.message_components as Comp

import jmcomic
from jmcomic import JmMagicConstants, JmModuleConfig
from jmcomic.jm_exception import (
    MissingAlbumPhotoException,
    RequestRetryAllFailException,
    PartialDownloadFailedException,
)
# DownloadCancelledException 在 jmcomic 2.7.6+ 才有，低版本兼容处理
try:
    from jmcomic.jm_exception import DownloadCancelledException
except ImportError:
    DownloadCancelledException = None
from jmcomic.jm_async_downloader import JmAsyncDownloader


# 库内置 advanced_retry 的请求级域名轮询配置：
# 最多轮询 3 轮，单个域名失败 3 次后暂时跳过；按历史失败次数排序，失败多的后置。
# 整本下载仍由 _download_album_progress 保留 3 次外层任务重试，二者不冲突。
ADVANCED_RETRY_CONFIG = {
    "retry_rounds": 3,
    "retry_domain_max_times": 3,
}


class ProgressDownloader(JmAsyncDownloader):
    """带进度跟踪的下载器，进度存在类变量中按 album_id 隔离
    适配 jmcomic 2.7.9+ 的异步下载器架构（async 上下文管理器 + 异步钩子）
    """
    # key: album_id, value: 进度状态
    _progress = {}
    # key: album_id, value: 本次下载实际写入的漫画根目录（不依赖 dir_rule 命名）
    _album_dirs = {}

    @staticmethod
    def _album_page_count(album):
        """优先使用 album.page_count；旧的逐章累加在 before_album 时 page_arr 尚未拉取。"""
        try:
            total = int(getattr(album, "page_count", 0) or 0)
        except (TypeError, ValueError):
            total = 0
        if total > 0:
            return total
        # 兜底：若调用方已经预取过 page_arr，仍可累加出总数
        for photo in album:
            total += len(getattr(photo, "page_arr", None) or [])
        return max(0, total)

    async def before_album(self, album):
        await super().before_album(album)
        try:
            photo_total = len(album)
        except Exception:
            photo_total = 0
        ProgressDownloader._progress[str(album.album_id)] = {
            'total': self._album_page_count(album),
            'done': 0,
            'finished': False,
            'photo_total': photo_total,
            'photo_done': 0,
            'current_photo': 0,
            'current_name': '',
        }

    async def before_photo(self, photo):
        await super().before_photo(photo)
        try:
            state = ProgressDownloader._progress.get(str(photo.album_id))
            if state is None:
                return
            state['photo_done'] += 1
            try:
                state['current_photo'] = int(getattr(photo, 'index', 0) or state['photo_done'])
            except (TypeError, ValueError):
                state['current_photo'] = state['photo_done']
            state['current_name'] = str(getattr(photo, 'name', '') or '')
            # album.page_count 不可用时，至少用已预取章节补一个可见的进度分母
            if state['total'] <= 0:
                state['total'] += len(getattr(photo, 'page_arr', None) or [])
        except Exception:
            pass

    async def after_image(self, image, img_save_path):
        await super().after_image(image, img_save_path)
        try:
            album_id = str(image.from_photo.album_id)
            state = ProgressDownloader._progress.get(album_id)
            if state is not None:
                state['done'] += 1
                if state['total'] > 0:
                    state['done'] = min(state['done'], state['total'])

            # 记录实际保存目录：取 base_dir 下的第一层目录，无论 dir_rule 怎么配都能定位
            base_dir = Path(self.option.dir_rule.base_dir).resolve()
            rel_path = Path(img_save_path).resolve().relative_to(base_dir)
            if rel_path.parts:
                ProgressDownloader._album_dirs[album_id] = str(base_dir / rel_path.parts[0])
        except Exception:
            pass

    async def after_album(self, album):
        await super().after_album(album)
        aid = str(album.album_id)
        state = ProgressDownloader._progress.get(aid)
        if state is not None:
            state['finished'] = True
            if state['total'] > 0:
                state['done'] = state['total']


class PageUnit(NamedTuple):
    """一个待打包的页段：章节内第 start~end 页（1 起，含两端）"""

    index: int
    photo: Any
    start: int
    end: int
    total: int

    @property
    def pages(self):
        return max(0, self.end - self.start + 1)

    @property
    def partial(self):
        return self.start > 1 or self.end < self.total


@register("astrbot_plugin_jmcomic", "wnzmb", "漫画下载插件", "0.7.8")
class JmComicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict[str, Any] | None = None):
        super().__init__(context)
        self.config = config or {}

        # 路径配置：放到 plugin_data 下，插件更新不会丢
        # 插件目录: .../data/plugins/astrbot_plugin_jmcomic/
        # 往上两级到 data/，再进 plugin_data/astrbot_plugin_jmcomic/
        plugin_dir = Path(os.path.dirname(__file__))
        self.data_dir = plugin_dir.parent.parent / "plugin_data" / "astrbot_plugin_jmcomic"
        self.download_base_dir = self.data_dir / "downloads"
        self.zip_base_dir = self.data_dir / "zips"
        self.download_base_dir.mkdir(parents=True, exist_ok=True)
        self.zip_base_dir.mkdir(parents=True, exist_ok=True)

        # 从插件配置读取参数
        username = self.config.get("username", "")
        password = self.config.get("password", "")
        domain_str = self.config.get("domain", "")
        # 支持多个域名，逗号分隔
        custom_domains = [d.strip() for d in domain_str.split(",") if d.strip()]
        photo_threads = int(self.config.get("photo_threads", 2))
        image_threads = int(self.config.get("image_threads", 5))
        self.per_page = int(self.config.get("per_page", 10))
        self.allow_group = self.config.get("allow_group", True)
        # 群白名单：逗号分隔的群号；为空时沿用 allow_group 的全局开关
        group_raw = str(self.config.get("enabled_groups", "") or "")
        group_raw = (
            group_raw.replace("，", ",").replace(" ", ",")
            .replace("\n", ",").replace("\t", ",")
        )
        self.enabled_groups = {x.strip() for x in group_raw.split(",") if x.strip()}
        self.numeric_search_as_album = bool(self.config.get("numeric_search_as_album", False))
        # 订阅更新检查间隔（秒），0 表示关闭后台检查；命令仍可正常增删订阅
        self.subscribe_interval = max(0, int(self.config.get("subscribe_interval", 3600) or 0))
        self.convert_enabled = self.config.get("convert_enabled", True)
        self.convert_title = self.config.get("convert_title", True)
        self.convert_tags = self.config.get("convert_tags", True)
        self.convert_author = self.config.get("convert_author", True)
        self.convert_comment = self.config.get("convert_comment", True)
        self.sensitive_filter = self.config.get("sensitive_filter", True)
        self.tease_enabled = self.config.get("tease_enabled", False)
        self.cache_days = int(self.config.get("cache_days", 7))
        self.comment_count = int(self.config.get("comment_count", 10))
        self.history_max = max(0, int(self.config.get("history_max", 0) or 0))
        self.dual_search = self.config.get("dual_search", True)

        # 号主与文件发送限额（方案B：账号类命令只允许号主）
        owner_raw = str(self.config.get("owner_ids", "") or "")
        owner_raw = owner_raw.replace("，", ",").replace(" ", ",").replace("\n", ",").replace("\t", ",")
        self.owner_ids = {x.strip() for x in owner_raw.split(",") if x.strip()}
        self.daily_file_limit = max(0, int(self.config.get("daily_file_limit", 0) or 0))
        self.hourly_file_limit = max(0, int(self.config.get("hourly_file_limit", 0) or 0))
        self.send_interval = max(0.0, float(self.config.get("send_interval", 1.5)))
        # ZIP 按章数合并；PDF 按页数合并，两者互不影响。
        # 旧配置 pdf_group_size 仅作为 ZIP 合并章数的兼容回退。
        configured_group = int(
            self.config.get("zip_group_size", self.config.get("pdf_group_size", 5)) or 0
        )
        self.zip_group_size = min(50, max(1, configured_group or 5))
        configured_pages = int(self.config.get("pdf_page_limit", 100) or 0)
        self.pdf_page_limit = min(2000, max(1, configured_pages or 100))
        # ZIP 单章页数上限，0 表示不按页数切分，只按章数合并
        configured_zip_pages = int(self.config.get("zip_page_limit", 300) or 0)
        self.zip_page_limit = min(2000, max(0, configured_zip_pages))
        # 整本下载进度：每 N% 推送一次，0 表示关闭；60 秒心跳为内部常量
        self.progress_step = min(100, max(0, int(self.config.get("progress_step", 10) or 0)))
        # 代理：use_proxy=false 时显式传空 dict，避免系统代理环境变量干扰
        proxy_url = str(self.config.get("proxy_url", "") or "").strip()
        self.use_proxy = bool(self.config.get("use_proxy", False)) and bool(proxy_url)
        if bool(self.config.get("use_proxy", False)) and not proxy_url:
            logger.warning("已开启 use_proxy，但 proxy_url 为空，本次按直连处理")
        self.proxy_url = proxy_url
        self.send_state_file = self.data_dir / "send_limits.json"
        self._id_type_cache = {}
        # 当前用户正在执行的下载任务，供 /jmstop 取消
        self._download_tasks: dict[str, set] = {}
        # 收藏夹导出二次确认：key=(发送者, umo, 操作) -> 首次输入时间
        self._pending_exports: dict[tuple, float] = {}
        # 订阅更新后台检查任务
        self._subscribe_task = None
        self._subscription_lock = None

        # 客户端类型：api（默认，功能全）或 html（支持分类筛选）
        self.client_type = self.config.get("client_type", "api").lower()
        if self.client_type not in ("api", "html"):
            self.client_type = "api"

        # 解析用户自定义敏感词
        self.custom_sensitive_map = {}
        custom_sensitive = self.config.get("custom_sensitive", "")
        for line in custom_sensitive.strip().split("\n"):
            line = line.strip()
            if "=" in line:
                word, pinyin = line.split("=", 1)
                word = word.strip()
                pinyin = pinyin.strip()
                if word and pinyin:
                    self.custom_sensitive_map[word] = pinyin

        # 启动时自动清理过期缓存
        if self.cache_days > 0:
            import time as _time
            now = _time.time()
            for item in self.download_base_dir.iterdir():
                if item.is_dir() and not item.name.startswith('.'):
                    mtime = item.stat().st_mtime
                    if now - mtime > self.cache_days * 86400:
                        shutil.rmtree(item, ignore_errors=True)
                        logger.info(f"自动清理过期缓存: {item.name}")

        # 动态生成 jmcomic 配置
        # 根据用户配置选择客户端类型：
        #   api  = API客户端（移动端接口），功能全：收藏添加、评论翻页、签到、用户信息，不支持分类筛选
        #   html = HTML客户端（网页端接口），支持分类筛选，但收藏添加报错、评论不翻页、总数解析不出
        if self.client_type == "api":
            # 用户自定义域名优先，随后追加 jmcomic 内置的 API 域名作为兜底
            api_domains = list(custom_domains)
            for d in JmModuleConfig.DOMAIN_API_LIST:
                if d not in api_domains:
                    api_domains.append(d)
            domain_conf = {"api": api_domains}
        else:
            # HTML 域名未配置时交给 jmcomic 自动获取，避免在代码里硬编码
            domain_conf = {"html": custom_domains}

        # 明确覆盖系统代理：未启用时传空 dict，启用时 http/https 都走同一代理。
        proxy_map = {"http": self.proxy_url, "https": self.proxy_url} if self.use_proxy else {}

        option_dict = {
            "log": False,
            "client": {
                "impl": self.client_type,
                "retry_times": 5,
                "cache": True,
                "domain": domain_conf,
                "postman": {
                    "meta_data": {
                        "proxies": proxy_map,
                        "timeout": 15
                    }
                }
            },
            "download": {
                "cache": True,
                "image": {
                    "decode": True,
                    "suffix": ".jpg"
                },
                "threading": {
                    "image": image_threads,
                    "photo": photo_threads
                }
            },
            "dir_rule": {
                # 缓存目录只保留 ID，标题写入 metadata.json，避免长标题触发
                # Windows MAX_PATH；目录结构：JM{漫画id}/P{章节id}/图片
                "rule": "Bd/JM{Aid}/P{Pid}",
                "normalize_zh": "zh-cn"
            },
            "plugins": {
                "after_init": []
            }
        }

        # 请求级域名轮询放在登录之前，确保登录与后续 client 都应用该策略。
        # 同步客户端（搜索/详情/单章下载等）走 advanced_retry；
        # 异步整本下载器自带域名轮询，外层仍有整任务 3 次重试。
        option_dict["plugins"]["after_init"].append({
            "plugin": "advanced_retry",
            "kwargs": {
                "retry_config": dict(ADVANCED_RETRY_CONFIG)
            }
        })

        # 如果填了账号密码，加登录插件
        if username and password:
            option_dict["plugins"]["after_init"].append({
                "plugin": "login",
                "kwargs": {
                    "username": username,
                    "password": password
                }
            })

        # 转成 yml 字符串加载
        import yaml
        option_str = yaml.dump(option_dict, allow_unicode=True)
        self.option = jmcomic.create_option_by_str(option_str)
        self.option.dir_rule.base_dir = str(self.download_base_dir)

        # 登录态客户端缓存：避免每次命令都重新登录
        self._cached_client = None
        self._client_lock = None  # 延迟创建，避免__init__时event_loop未就绪

        # 排序方式映射
        self.order_map = {
            'mr': '最新',
            'mv': '最多浏览',
            'mp': '最多页数',
            'tf': '最多收藏',
            'tr': '评分最高',
            'md': '最多评论',
        }

        logger.info(
            f"JMComic 插件初始化完成 v0.7.8，客户端: {self.client_type.upper()}，"
            f"镜像站: {custom_domains}，代理: {'已启用' if self.use_proxy else '直连'}"
        )

        # 历史记录文件
        self.history_file = self.data_dir / "history.json"
        if not self.history_file.exists():
            self.history_file.write_text("{}", encoding="utf-8")

        # 最近一次成功签到的本地记录（API 签到日历更新有延迟时兜底）
        self.checkin_file = self.data_dir / "checkin_state.json"

        # 订阅更新：按会话 umo 保存漫画快照
        self.subscription_file = self.data_dir / "subscriptions.json"
        if not self.subscription_file.exists():
            self.subscription_file.write_text("{}", encoding="utf-8")

    def _check_permission(self, event: AstrMessageEvent):
        """检查群聊权限：allow_group 总开关 + enabled_groups 白名单"""
        group_id = event.get_group_id()
        if not group_id:
            # 私聊不受群开关和白名单影响
            return True
        if not self.allow_group:
            return False
        if self.enabled_groups and str(group_id) not in self.enabled_groups:
            return False
        return True

    def _permission_denied_text(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        if group_id and not self.allow_group:
            return "本插件已关闭群聊使用，请私聊使用"
        return (
            "本群不在插件群白名单中，请私聊使用，"
            "或让管理员把本群号加入 enabled_groups"
        )

    # ========== 号主与文件发送限额 ==========
    def _is_owner(self, event: AstrMessageEvent) -> bool:
        """方案B：只有配置在 owner_ids 里的发送者才能使用账号类命令"""
        if not self.owner_ids:
            return False
        return str(event.get_sender_id()) in self.owner_ids

    def _owner_required_text(self) -> str:
        if not self.owner_ids:
            return (
                "⚠️ 账号类命令仅限 JM 账号号主使用。\n"
                "请先在插件配置里填写 owner_ids（号主ID），"
                "发送 /jmwhoami 可查看你的ID。"
            )
        return "⚠️ 账号类命令仅限 JM 账号号主使用。"

    def _load_send_times(self):
        try:
            data = json.loads(self.send_state_file.read_text(encoding="utf-8"))
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

    def _save_send_times(self, times):
        try:
            now = datetime.now()
            cutoff = now - timedelta(hours=24)
            times = sorted(t for t in times if t >= cutoff)
            payload = {"times": [t.strftime("%Y-%m-%dT%H:%M:%S") for t in times]}
            self.send_state_file.write_text(json.dumps(payload), encoding="utf-8")
        except Exception as e:
            logger.debug(f"保存发送限额状态失败: {e}")

    def _file_quota(self):
        """返回今日/最近1小时的文件发送额度（本地统计，只统计本插件）

        限额配置为 0 时表示不限制，remaining 返回 None。
        """
        now = datetime.now()
        times = self._load_send_times()
        hour_ago = now - timedelta(hours=1)
        hour_count = sum(1 for t in times if t >= hour_ago)
        day_count = sum(1 for t in times if t.date() == now.date())
        return {
            "hour_count": hour_count,
            "hour_remaining": (
                None if self.hourly_file_limit <= 0
                else max(0, self.hourly_file_limit - hour_count)
            ),
            "day_count": day_count,
            "day_remaining": (
                None if self.daily_file_limit <= 0
                else max(0, self.daily_file_limit - day_count)
            ),
        }

    @staticmethod
    def _quota_reached(quota):
        """任一限额已用完；None 表示该限额不限制"""
        return any(
            quota[key] is not None and quota[key] <= 0
            for key in ("hour_remaining", "day_remaining")
        )

    @staticmethod
    def _quota_display(count, limit):
        return f"{count}/不限" if limit <= 0 else f"{count}/{limit}"

    @staticmethod
    def _limit_text(limit):
        return "不限" if limit <= 0 else str(limit)

    def _zip_page_hint(self):
        if self.zip_page_limit <= 0:
            return "页数不限"
        return f"每册不超过 {self.zip_page_limit} 页"

    def _record_file_send(self, count=1):
        now = datetime.now()
        times = self._load_send_times()
        times.extend([now] * max(0, count))
        self._save_send_times(times)

    def _quota_block_text(self, remaining_files=0):
        q = self._file_quota()
        if q["day_remaining"] is not None and q["day_remaining"] <= 0:
            return (
                f"⏳ 今日文件发送额度已用完（{q['day_count']}/{self.daily_file_limit}）。\n"
                f"剩余 {remaining_files} 个文件未发送，请明天再试。\n"
                "已下载的图片会保留缓存，重新执行命令不会重新下载。"
            )
        if q["hour_remaining"] is not None and q["hour_remaining"] <= 0:
            return (
                f"⏳ 最近1小时文件发送额度已用完（{q['hour_count']}/{self.hourly_file_limit}）。\n"
                f"剩余 {remaining_files} 个文件未发送，请至少等待约 1 小时后再试。\n"
                "已下载的图片会保留缓存，重新执行命令不会重新下载。"
            )
        return ""

    def _quota_status_text(self):
        q = self._file_quota()
        return (
            f"今日文件 {self._quota_display(q['day_count'], self.daily_file_limit)}，"
            f"最近1小时 {self._quota_display(q['hour_count'], self.hourly_file_limit)}"
        )

    def _handle_jm_exception(self, e: Exception, action: str = "操作") -> str:
        """把 jmcomic 异常转换为用户友好的提示"""
        if isinstance(e, MissingAlbumPhotoException):
            return f"❌ 查无此本/章节，请检查 ID 是否正确"
        elif isinstance(e, RequestRetryAllFailException):
            return (
                "❌ 网络请求全部失败，可尝试在插件配置中更换域名；"
                "如已启用代理，请检查 proxy_url 是否可连通\n"
                f"{e}"
            )
        elif isinstance(e, PartialDownloadFailedException):
            return f"⚠️ 部分图片下载失败，已下载图片会保留缓存，请稍后重试"
        elif DownloadCancelledException is not None and isinstance(e, DownloadCancelledException):
            return f"⏹️ 下载已取消"
        else:
            logger.error(f"{action}失败: {e}", exc_info=True)
            return f"❌ {action}失败：{e}"

    def _new_logged_client(self):
        """创建客户端；若配置了账号密码则同步登录一次，确保会员登录态。
        带缓存：复用已登录的客户端，避免每次命令都重新登录。

        签到、收藏夹、搜索等接口需要登录态：
        - jmcomic 登录插件只把 cookies 写回 option，不传播 _user_id；
          新 client 的 _user_id 恒为 None，daily_checkin/get_daily 会直接报
          "签到需要传入 user_id 参数，或者先调用 login 方法"。
        - 登录失败在插件初始化阶段会被 safe=True 吞掉，导致后续请求 401。
        这里在每次创建客户端时主动 login 一次，并给出可操作的错误提示。
        """
        # 优先返回缓存的已登录客户端
        if self._cached_client is not None:
            return self._cached_client

        client = self.option.new_jm_client()
        username = self.config.get("username", "")
        password = self.config.get("password", "")
        if username and password:
            try:
                client.login(username, password)
                logger.info("JMComic 客户端登录成功")
            except Exception as e:
                raise Exception(f"登录失败，请检查账号密码或镜像域名配置：{e}")

        # 缓存已登录客户端，后续命令复用
        self._cached_client = client
        return client

    def _invalidate_client(self):
        """客户端登录态失效时调用，下次请求会重新登录"""
        self._cached_client = None

    def _to_simplified(self, text):
        """繁体转简体"""
        if not self.convert_enabled or not text:
            return text
        try:
            import zhconv
            return zhconv.convert(text, 'zh-cn')
        except:
            return text

    def _to_traditional(self, text):
        """简体转繁体，用于搜索"""
        if not text:
            return text
        try:
            import zhconv
            return zhconv.convert(text, 'zh-tw')
        except:
            return text

    def _album_dir_has_images(self, folder: Path) -> bool:
        """判断目录里是否真的有图片（避免把空目录/半成品当作已下载）"""
        try:
            for pattern in ("*.jpg", "*.jpeg", "*.png", "*.webp", "*.gif"):
                for _ in folder.rglob(pattern):
                    return True
        except Exception:
            pass
        return False

    def _find_album_dir(self, album_id):
        """按ID查找已下载的漫画目录。
        优先使用下载过程中记录的真实目录，其次按目录名前缀匹配，
        不再依赖 album.save_path（jmcomic 2.7.9 默认 dir_rule 会返回基础目录）。
        返回 Path 或 None
        """
        album_id = str(album_id).strip()
        base_resolved = self.download_base_dir.resolve()

        # 1) 下载钩子记录的目录最准确
        recorded = ProgressDownloader._album_dirs.get(album_id)
        if recorded:
            folder = Path(recorded)
            if folder.is_dir() and self._album_dir_has_images(folder):
                return folder

        # 2) 新版目录是精确的 JM{id}；旧版标题目录作为兜底
        candidates = []
        exact = self.download_base_dir / f"JM{album_id}"
        if exact.is_dir() and exact.resolve() != base_resolved:
            candidates.append(exact)
        legacy = [
            folder for folder in self.download_base_dir.glob(f"JM{album_id}_*")
            if folder.is_dir() and folder.resolve() != base_resolved
        ]
        legacy.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        candidates.extend(legacy)
        for folder in candidates:
            if self._album_dir_has_images(folder):
                return folder

        return None

    @staticmethod
    def _metadata_file(folder):
        return Path(folder) / "metadata.json" if folder is not None else None

    def _read_metadata(self, folder):
        """读取缓存目录的 metadata.json，失败时返回空字典"""
        if folder is None:
            return {}
        try:
            data = json.loads(self._metadata_file(folder).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_metadata(self, folder, data):
        """原子写入 metadata.json，失败不影响下载和打包"""
        if folder is None:
            return False
        try:
            folder = Path(folder)
            folder.mkdir(parents=True, exist_ok=True)
            path = self._metadata_file(folder)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, path)
            return True
        except Exception as e:
            logger.warning(f"写入缓存元数据失败: {e}")
            return False

    @staticmethod
    def _text_list(value):
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        return [str(item) for item in value if str(item).strip()]

    def _album_display_title(self, album, album_dir=None):
        name = getattr(album, "name", "") if album is not None else ""
        if name:
            return self._safe_text(str(name), self.convert_title)
        metadata = self._read_metadata(album_dir)
        return metadata.get("title_display") or metadata.get("title") or ""

    def _chapter_display_title(self, photo, index=1, folder=None, album_dir=None):
        name = getattr(photo, "name", "") if photo is not None else ""
        if name:
            return self._safe_text(str(name), self.convert_title)

        metadata = self._read_metadata(folder)
        if metadata.get("title_display") or metadata.get("title"):
            return metadata.get("title_display") or metadata.get("title")

        photo_id = str(getattr(photo, "photo_id", "") or "")
        album_metadata = self._read_metadata(album_dir)
        for item in album_metadata.get("chapters", []) or []:
            if photo_id and str(item.get("photo_id", "")) == photo_id:
                return item.get("title_display") or item.get("title") or f"第{index}章"
        return f"第{index}章"

    def _save_target_metadata(self, album, album_id, chapters, album_dir,
                              selected_photos=None):
        """把漫画/章节标题写入缓存目录，供打包和缺标题兜底使用"""
        if album_dir is None:
            return
        all_chapters = list(chapters or [])
        if album is not None:
            try:
                expanded = list(album)
                if expanded:
                    all_chapters = expanded
            except Exception:
                pass

        album_id = str(
            album_id or getattr(album, "album_id", "") or ""
        )
        existing = self._read_metadata(album_dir)
        title = str(
            (getattr(album, "name", "") if album is not None else "")
            or existing.get("title", "")
            or ""
        )
        title_display = self._safe_text(title, self.convert_title) if title else ""
        authors = self._text_list(
            (getattr(album, "authors", None) if album is not None else None)
            or existing.get("authors", [])
        )
        tags = self._text_list(
            (getattr(album, "tags", None) if album is not None else None)
            or existing.get("tags", [])
        )
        author_display = [
            self._safe_text(item, self.convert_author) for item in authors
        ]
        tag_display = [
            self._safe_text(item, self.convert_tags) for item in tags
        ]

        existing_chapters = {
            str(item.get("photo_id", "")): item
            for item in (existing.get("chapters", []) or [])
            if isinstance(item, dict)
        }
        chapter_items = []
        for index, photo in enumerate(all_chapters, 1):
            photo_title = str(getattr(photo, "name", "") or "")
            photo_id = str(getattr(photo, "photo_id", "") or "")
            previous = existing_chapters.get(photo_id, {})
            chapter_title_display = (
                self._safe_text(photo_title, self.convert_title)
                if photo_title else previous.get("title_display", "")
            )
            item = {
                "photo_id": photo_id,
                "index": index,
                "title": photo_title or previous.get("title", ""),
                "title_display": chapter_title_display,
            }
            if previous.get("page_count") is not None:
                item["page_count"] = previous["page_count"]
            chapter_items.append(item)

        album_data = {
            "schema": 1,
            "album_id": album_id,
            "title": title,
            "title_display": title_display,
            "authors": authors,
            "authors_display": author_display,
            "tags": tags,
            "tags_display": tag_display,
            "chapters": chapter_items,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        # 先写入专辑信息，章节标题为空时 _chapter_display_title 可从
        # 专辑元数据里回退；写入章节元数据后再补全专辑里的标题。
        self._write_metadata(album_dir, album_data)

        if selected_photos is None:
            selected_photos = all_chapters
        seen = set()
        album_items_changed = False
        for photo in selected_photos:
            photo_id = str(getattr(photo, "photo_id", "") or "")
            if not photo_id or photo_id in seen:
                continue
            seen.add(photo_id)
            folder = self._chapter_dir(photo, album_dir)
            if folder is None:
                continue
            index = next(
                (
                    item["index"] for item in chapter_items
                    if item.get("photo_id") == photo_id
                ),
                1,
            )
            chapter_title = self._chapter_display_title(
                photo, index, folder, album_dir
            )
            page_arr = getattr(photo, "page_arr", None)
            page_count = len(page_arr) if page_arr else len(self._chapter_images(folder))
            raw_chapter_title = str(getattr(photo, "name", "") or "")
            self._write_metadata(folder, {
                "schema": 1,
                "album_id": album_id,
                "album_title": title,
                "album_title_display": title_display,
                "photo_id": photo_id,
                "index": index,
                "title": raw_chapter_title,
                "title_display": chapter_title,
                "page_count": page_count,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            })
            for item in chapter_items:
                if item.get("photo_id") != photo_id:
                    continue
                if raw_chapter_title:
                    item["title"] = raw_chapter_title
                item["title_display"] = chapter_title
                item["page_count"] = page_count
                album_items_changed = True
                break

        if album_items_changed:
            album_data["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self._write_metadata(album_dir, album_data)

    def _unit_metadata_index(self, unit, folder):
        try:
            index = int(self._read_metadata(folder).get("index") or unit.index)
            return index if index > 0 else unit.index
        except Exception:
            return unit.index

    def _album_info_text(self, album, album_id, album_dir, chapter_entries):
        """生成放进 ZIP 的可读漫画信息文件"""
        metadata = self._read_metadata(album_dir)
        title = self._album_display_title(album, album_dir) or f"JM{album_id}"
        authors = self._text_list(
            (getattr(album, "authors", None) if album is not None else None)
            or metadata.get("authors", [])
        )
        tags = self._text_list(
            (getattr(album, "tags", None) if album is not None else None)
            or metadata.get("tags", [])
        )
        lines = [
            "漫画信息",
            f"标题：{title}",
            f"ID：JM{album_id}",
        ]
        if authors:
            lines.append("作者：" + "、".join(
                self._safe_text(item, self.convert_author) for item in authors
            ))
        if tags:
            lines.append("标签：" + "、".join(
                self._safe_text(item, self.convert_tags) for item in tags
            ))
        lines.append("")
        lines.append("本次包含：")
        if not chapter_entries:
            lines.append("- 无可用章节")
        for unit, folder, _images in chapter_entries:
            chapter_index = self._unit_metadata_index(unit, folder)
            chapter_title = self._chapter_display_title(
                unit.photo, chapter_index, folder, album_dir
            )
            page_suffix = (
                f"（第{unit.start}-{unit.end}页）" if unit.partial else ""
            )
            photo_id = getattr(unit.photo, "photo_id", chapter_index)
            lines.append(
                f"- 第{chapter_index:03d}章 {chapter_title}{page_suffix}  [JM{photo_id}]"
            )
        return "\n".join(lines).rstrip() + "\n"

    def _album_cover_text(self, album, album_id, album_dir, chapter_entries):
        """PDF 首页封面用的精简信息，避免章节过多时溢出"""
        metadata = self._read_metadata(album_dir)
        title = self._album_display_title(album, album_dir) or f"JM{album_id}"
        authors = self._text_list(
            (getattr(album, "authors", None) if album is not None else None)
            or metadata.get("authors", [])
        )
        tags = self._text_list(
            (getattr(album, "tags", None) if album is not None else None)
            or metadata.get("tags", [])
        )
        indexes = [
            self._unit_metadata_index(unit, folder)
            for unit, folder, _images in chapter_entries
        ]
        lines = [title, f"JM{album_id}"]
        if authors:
            lines.append("作者：" + "、".join(
                self._safe_text(item, self.convert_author) for item in authors
            ))
        if tags:
            lines.append("标签：" + "、".join(
                self._safe_text(item, self.convert_tags) for item in tags
            ))
        if indexes:
            index_text = self._format_index_token(indexes)
            lines.append(f"本次包含：第{index_text}章（共{len(set(indexes))}章）")
            partial_entries = [
                entry for entry in chapter_entries if entry[0].partial
            ]
            if partial_entries:
                lines.append("含页段：" + "、".join(
                    f"第{self._unit_metadata_index(unit, folder):03d}章 "
                    f"{unit.start}-{unit.end}页"
                    for unit, folder, _images in partial_entries[:6]
                ))
        return "\n".join(lines)

    def _pdf_metadata(self, album, album_id, album_dir, chapter_entries):
        metadata = self._read_metadata(album_dir)
        title = self._album_display_title(album, album_dir) or f"JM{album_id}"
        authors = self._text_list(
            (getattr(album, "authors", None) if album is not None else None)
            or metadata.get("authors", [])
        )
        tags = self._text_list(
            (getattr(album, "tags", None) if album is not None else None)
            or metadata.get("tags", [])
        )
        indexes = [
            self._unit_metadata_index(unit, folder)
            for unit, folder, _images in chapter_entries
        ]
        result = {
            "title": title,
            "subject": f"JM{album_id}"
            + (f" 第{self._format_index_token(indexes)}章" if indexes else ""),
        }
        if authors:
            result["author"] = "、".join(
                self._safe_text(item, self.convert_author) for item in authors
            )
        if tags:
            result["keywords"] = "、".join(
                self._safe_text(item, self.convert_tags) for item in tags
            )
        return result

    def _album_cache_complete(self, folder: Path, chapters) -> bool:
        """确认漫画目录下每个章节都有实际图片，避免半成品缓存被当成完整缓存"""
        if folder is None:
            return False
        chapters = list(chapters or [])
        if not chapters:
            return self._album_dir_has_images(folder)
        for photo in chapters:
            if self._chapter_dir(photo, folder) is None:
                return False
        return True

    @staticmethod
    def _looks_like_chapter_album(album) -> bool:
        """识别上游把章节 ID 当漫画 ID 返回的畸形 album 对象。

        API 对章节 ID 会返回 name=zip 文件名、authors/tags=['filename']
        且章节列表为父漫画的畸形数据；部分章节 ID（如 1236387）的
        authors/tags 为空，但首项 photo_id 会变成父漫画 ID。两种特征
        都用于提前给出可操作提示，避免 jmv/jmlist 展示错乱字段。
        """
        if album is None:
            return False
        authors = getattr(album, "authors", []) or []
        tags = getattr(album, "tags", []) or []
        if isinstance(authors, str):
            authors = [authors]
        if isinstance(tags, str):
            tags = [tags]
        authors = [str(x).strip().lower() for x in authors]
        tags = [str(x).strip().lower() for x in tags]
        if (authors and all(x == "filename" for x in authors)) or (
            tags and all(x == "filename" for x in tags)
        ):
            return True

        album_id = str(getattr(album, "album_id", "") or "").strip()
        try:
            first = next(iter(album))
        except (TypeError, StopIteration):
            return False
        first_id = str(getattr(first, "photo_id", "") or "").strip()
        return bool(album_id and first_id and first_id != album_id)

    @staticmethod
    def _chapter_album_parent_id(album, album_id) -> str:
        """从畸形章节 album 的首项提取父漫画ID。

        API 返回的畸形章节 album 里，第一项通常是父漫画的占位章节，
        其 photo_id 就是父漫画ID（例如 275013 的首项 photo_id=43164）。
        """
        try:
            first = next(iter(album))
        except (TypeError, StopIteration):
            return ""
        candidate = str(getattr(first, "photo_id", "") or "").strip()
        if candidate.isdigit() and candidate != str(album_id):
            return candidate
        return ""

    async def _resolve_chapter_album_parent(self, client, album, album_id) -> str:
        """优先从 album 首项取父漫画ID，失败时用 photo 详情兜底"""
        parent_id = self._chapter_album_parent_id(album, album_id)
        if parent_id:
            return parent_id
        try:
            photo = await asyncio.to_thread(client.get_photo_detail, album_id)
        except Exception:
            return ""
        parent_id = str(getattr(photo, "album_id", "") or "").strip()
        if parent_id.isdigit() and parent_id != str(album_id):
            return parent_id
        return ""

    def _load_history(self):
        """加载下载历史，结构是 {user_id: [历史列表]}"""
        try:
            data = json.loads(self.history_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
            else:
                return {}
        except:
            return {}

    def _save_history(self, data):
        """保存下载历史"""
        self.history_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _get_user_history(self, user_id):
        data = self._load_history()
        return data.get(user_id, [])

    def _add_user_history(self, user_id, item):
        data = self._load_history()
        if user_id not in data:
            data[user_id] = []
        data[user_id].insert(0, item)
        # history_max 为 0 表示保留全部历史
        if self.history_max > 0:
            data[user_id] = data[user_id][:self.history_max]
        self._save_history(data)
        return len(data[user_id])

    def _sanitize_tags(self, tags):
        """敏感词替换成拼音，避免QQ审核"""
        if not self.sensitive_filter:
            return tags
        import re
        from pypinyin import lazy_pinyin

        sensitive_map = {}
        # 合并用户自定义敏感词
        sensitive_map.update(self.custom_sensitive_map)
        result = []
        for tag in tags:
            # 先替换已知敏感词
            for cn, pinyin in sensitive_map.items():
                if cn in tag:
                    tag = tag.replace(cn, pinyin)

            # 匹配 "X交" 和 "X乳"，只把这两个字转拼音
            def replace_jiao(match):
                word = match.group(0)
                return ''.join(lazy_pinyin(word))
            def replace_ru(match):
                word = match.group(0)
                return ''.join(lazy_pinyin(word))

            tag = re.sub(r'.交', replace_jiao, tag)
            tag = re.sub(r'.乳', replace_ru, tag)

            result.append(tag)
        return result

    def _safe_text(self, text, simplify=None):
        """统一处理所有对外展示的动态文本：繁简转换 + 敏感词转拼音"""
        if not text:
            return text
        if not isinstance(text, str):
            text = str(text)
        if simplify is None:
            simplify = self.convert_title
        if simplify:
            text = self._to_simplified(text)
        if self.sensitive_filter:
            text = self._sanitize_tags([text])[0]
        return text

    # 账号类命令不经过全局敏感词转换，避免把用户 ID/账号信息误处理
    _ACCOUNT_COMMANDS = {"jmcheckin", "jmme", "jmfav", "jmwhoami"}

    def _command_name(self, event):
        try:
            first = (getattr(event, "message_str", "") or "").strip().split()[0]
            return first[1:] if first.startswith("/") else first
        except Exception:
            return ""

    def _output_text(self, event, text):
        """所有非账号类输出统一做繁简/敏感词处理"""
        if self._command_name(event) in self._ACCOUNT_COMMANDS:
            return text
        return self._safe_text(text)

    def _plain(self, event, text):
        return event.plain_result(self._output_text(event, text))

    def _chain(self, event, chain):
        """处理 chain 中的文本组件，文件/图片组件保持原样"""
        if self._command_name(event) in self._ACCOUNT_COMMANDS:
            return event.chain_result(chain)
        safe_chain = []
        for component in chain:
            if isinstance(component, Comp.Plain):
                safe_chain.append(Comp.Plain(self._output_text(event, getattr(component, "text", ""))))
            else:
                safe_chain.append(component)
        return event.chain_result(safe_chain)

    def _safe_filename(self, name, max_len=60):
        """生成安全的文件名：敏感词转拼音并去掉非法字符"""
        name = self._safe_text(name) or ''
        # 章节名本身可能带 .zip/.pdf 等后缀，避免生成 .zip.zip / .zip.pdf
        while True:
            stripped = re.sub(
                r"(?i)\.(?:zip|pdf|rar|7z|jpg|jpeg|png|webp|gif|mp4|mkv|epub|txt)$",
                "",
                name.strip(),
            )
            if stripped == name:
                break
            name = stripped
        for ch in '\\/:*?"<>|\n\r\t':
            name = name.replace(ch, '_')
        return name.strip()[:max_len] or 'untitled'

    # ========== QQ 消息违规检测 ==========
    def _backend_log_path(self):
        """定位 AstrBot 的 backend.log，用于检测平台静默拦截"""
        candidates = []
        try:
            # .../.astrbot/data/plugin_data/<plugin> -> .../.astrbot/logs/backend.log
            candidates.append(self.data_dir.parents[2] / "logs" / "backend.log")
            candidates.append(self.data_dir.parents[1] / "logs" / "backend.log")
        except Exception:
            pass
        candidates.append(Path("logs") / "backend.log")
        for path in candidates:
            try:
                if path.exists():
                    return path
            except Exception:
                continue
        return None

    def _log_pos(self):
        path = self._backend_log_path()
        if path is None:
            return 0
        try:
            return path.stat().st_size
        except Exception:
            return 0

    def _log_has_violation(self, offset):
        """从 offset 开始读日志，检测“消息内容违规”类错误"""
        path = self._backend_log_path()
        if path is None:
            return False
        try:
            size = path.stat().st_size
            if offset <= 0 or offset > size:
                offset = max(0, size - 65536)
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                f.seek(offset)
                text = f.read()
        except Exception:
            return False
        return any(k in text for k in ("消息内容违规", "内容违规", "40034006"))

    @staticmethod
    def _is_violation_error(err):
        """递归检查发送异常及 tenacity 重试原因里是否包含违规关键词"""
        texts = []
        cur = err
        for _ in range(6):
            if cur is None:
                break
            try:
                texts.append(str(cur))
            except Exception:
                pass
            last_attempt = getattr(cur, "last_attempt", None)
            if last_attempt is not None:
                try:
                    texts.append(str(last_attempt.exception()))
                except Exception:
                    pass
            cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
        text = " ".join(texts)
        return any(k in text for k in ("消息内容违规", "内容违规", "40034006"))

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

    # ========== 下载核心：ID识别 / 章节选择 / PDF-ZIP / 限额 ==========
    def _parse_download_args(self, args):
        """解析 /jm <目标> [-c 章节]；PDF/ZIP 合并参数由后台配置决定"""
        target = args[1] if len(args) > 1 else ""
        chapter_expr = None
        index = 2
        while index < len(args):
            token = args[index]
            if token in ("-c", "--chapters"):
                if index + 1 >= len(args):
                    raise ValueError("-c 后面需要填写章节或页段，例如 -c 31-40 或 -c 5:201-400")
                chapter_expr = args[index + 1]
                index += 2
            elif token.startswith("-"):
                raise ValueError(f"无法识别的选项：{token}")
            else:
                raise ValueError(f"无法识别的参数：{token}")
        return target, chapter_expr

    def _normalize_target(self, token):
        """支持 123 / JM123 / a123(漫画) / p123(章节)"""
        token = str(token).strip()
        prefix = ""
        # 负数/正号不是合法ID，不能从 "-1" 里提取出 "1"
        if token.startswith(("-", "+")):
            return prefix, token
        if len(token) > 1 and token[0].lower() in ("a", "p") and token[1:].isdigit():
            prefix = token[0].lower()
            token = token[1:]
        target_id = self._extract_id(token) or token
        return prefix, str(target_id)

    async def _resolve_target(self, client, token):
        """识别输入是漫画ID还是章节ID，并返回章节列表。"""
        prefix, target_id = self._normalize_target(token)
        if not target_id.isdigit():
            raise ValueError(f"无效ID：{token}")

        cache_key = f"{prefix}:{target_id}" if prefix else target_id
        kind = self._id_type_cache.get(cache_key)
        album_probe = None
        photo_probe = None

        if kind is None:
            if prefix == "a":
                kind = "album"
            elif prefix == "p":
                kind = "photo"
            else:
                # 先按章节探测：章节所属漫画ID与输入不同，说明输入是章节。
                # 首章ID可能与漫画ID相同，这种情况仍按漫画处理。
                try:
                    photo_probe = await asyncio.to_thread(client.get_photo_detail, target_id)
                    parent_id = str(getattr(photo_probe, "album_id", ""))
                    if parent_id and parent_id != target_id:
                        kind = "photo"
                    else:
                        album_probe = await asyncio.to_thread(client.get_album_detail, target_id)
                        kind = "album"
                except MissingAlbumPhotoException:
                    album_probe = await asyncio.to_thread(client.get_album_detail, target_id)
                    kind = "album"
            self._id_type_cache[cache_key] = kind

        if kind == "album":
            album = album_probe or await asyncio.to_thread(client.get_album_detail, target_id)
            chapters = list(album) if hasattr(album, "__iter__") else []
            return {
                "kind": "album",
                "id": str(target_id),
                "album": album,
                "photo": None,
                "chapters": chapters,
            }

        photo = photo_probe or await asyncio.to_thread(client.get_photo_detail, target_id)
        album = getattr(photo, "from_album", None)
        if album is None:
            parent_id = str(getattr(photo, "album_id", "") or "")
            if parent_id and parent_id != target_id:
                try:
                    album = await asyncio.to_thread(client.get_album_detail, parent_id)
                except Exception:
                    album = None
        return {
            "kind": "photo",
            "id": str(target_id),
            "album": album,
            "photo": photo,
            "chapters": [photo],
        }

    @staticmethod
    def _parse_page_range(part, notes):
        """解析页段：201-400 / 201- / 201，返回 (start, end|None)"""
        raw = str(part).strip()
        if not raw:
            notes.append("页码范围为空，已忽略")
            return None
        if "-" in raw:
            left, right = raw.split("-", 1)
            left, right = left.strip(), right.strip()
            if not left.isdigit() or (right and not right.isdigit()):
                notes.append(f"页码 {part} 格式不正确，已忽略")
                return None
            start = int(left)
            end = int(right) if right else None
            if start <= 0 or (end is not None and end <= 0):
                notes.append(f"页码 {part} 必须大于 0，已忽略")
                return None
            if end is not None and start > end:
                notes.append(f"页码 {part} 范围倒置，已按 {end}-{start} 处理")
                start, end = end, start
            return start, end
        if raw.isdigit():
            value = int(raw)
            if value <= 0:
                notes.append(f"页码 {part} 必须大于 0，已忽略")
                return None
            return value, value
        notes.append(f"页码 {part} 格式不正确，已忽略")
        return None

    @staticmethod
    def _merge_page_ranges(ranges):
        """只合并重叠或相邻的页段，保留不连续区间。

        入参/返回值为 [(起始页, 结束页或None)]；结束页为 None 表示到本章末。
        """
        values = []
        for item in ranges or []:
            try:
                start = int(item[0])
                end = None if item[1] is None else int(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if start <= 0:
                continue
            if end is not None and end < start:
                start, end = end, start
            if start <= 0:
                continue
            values.append((start, end))
        values.sort(key=lambda item: (item[0], item[1] is None))

        merged = []
        for start, end in values:
            if not merged:
                merged.append((start, end))
                continue
            last_start, last_end = merged[-1]
            if last_end is None:
                # 上一段已经覆盖到本章末，后续页段都被包含
                continue
            if start <= last_end + 1:
                if end is None or end > last_end:
                    merged[-1] = (last_start, end)
            else:
                merged.append((start, end))
        return merged

    def _select_chapters(self, chapters, expr):
        """解析 -c：2 / 2-4 / 2,5,7 / 5:201-400 / 5:1-5,5:10-15

        返回 ([(原序号, photo, 页段或None)], 无效项, 提示)
        页段为 (起始页, 结束页或None)，页码从 1 开始；同一章节的
        不连续页段会保留为多条记录，便于分别打包。
        """
        all_items = [(i + 1, ch, None) for i, ch in enumerate(chapters)]
        if not expr:
            return all_items, [], []

        indexes = set()
        whole_indexes = set()
        page_ranges = {}
        invalid = []
        notes = []
        for part in re.split(r"[,，]", str(expr)):
            part = part.strip()
            if not part:
                continue
            if ":" in part or "：" in part:
                chapter_part, page_part = re.split(r"[:：]", part, maxsplit=1)
                chapter_part = chapter_part.strip()
                if not chapter_part.isdigit():
                    invalid.append(part)
                    notes.append(f"{part} 页码范围只能用于单个章节，例如 5:201-400")
                    continue
                value = int(chapter_part)
                if value <= 0:
                    invalid.append(part)
                    continue
                page_range = self._parse_page_range(page_part, notes)
                if page_range is None:
                    invalid.append(part)
                    continue
                if value in whole_indexes:
                    notes.append(f"第{value}章已选择整章，忽略其页段 {part}")
                    continue
                page_ranges.setdefault(value, []).append(page_range)
                indexes.add(value)
            elif "-" in part:
                left, right = part.split("-", 1)
                if left.strip().isdigit() and right.strip().isdigit():
                    start, end = int(left), int(right)
                    if start <= 0 or end <= 0:
                        invalid.append(part)
                        continue
                    if start > end:
                        notes.append(f"{part} 范围倒置，已按 {end}-{start} 处理")
                        start, end = end, start
                    # 防止恶意超长范围占用内存；正常漫画章节数远小于此值
                    if end - start > 10000:
                        invalid.append(part)
                        notes.append(f"{part} 范围过大，已忽略")
                        continue
                    indexes.update(range(start, end + 1))
                    for value in range(start, end + 1):
                        whole_indexes.add(value)
                        page_ranges.pop(value, None)
                else:
                    invalid.append(part)
                    notes.append(f"{part} 不是有效章节格式，已忽略")
            elif part.isdigit():
                value = int(part)
                if value <= 0:
                    invalid.append(part)
                else:
                    indexes.add(value)
                    whole_indexes.add(value)
                    page_ranges.pop(value, None)
            else:
                invalid.append(part)
                notes.append(f"{part} 不是有效章节格式，已忽略")

        selected = []
        for index in sorted(indexes):
            if not 1 <= index <= len(chapters):
                invalid.append(str(index))
                continue
            if index in whole_indexes:
                selected.append((index, chapters[index - 1], None))
                continue
            for page_range in self._merge_page_ranges(page_ranges.get(index, [])):
                selected.append((index, chapters[index - 1], page_range))
        return selected, invalid, notes

    def _chapter_image_paths(self, photo, album_dir=None):
        """取章节已下载图片列表；未下载时返回空列表"""
        folder = self._chapter_dir(photo, album_dir)
        if folder is None:
            return []
        return self._chapter_images(folder)

    def _expand_page_units(self, selected, album_dir, page_limit):
        """把选中章节展开成页段。

        完整章节是一个页段；只有单章超过 page_limit 时才切成多个页段。
        """
        units = []
        for index, photo, page_range in selected:
            total = len(self._chapter_image_paths(photo, album_dir))
            if total <= 0:
                units.append(PageUnit(index, photo, 1, 0, 0))
                continue
            start, end = 1, total
            if page_range:
                start = max(1, min(int(page_range[0]), total))
                end = total if page_range[1] is None else min(int(page_range[1]), total)
                if end < start:
                    start, end = end, start
            if page_limit and page_limit > 0:
                cursor = start
                while cursor <= end:
                    chunk_end = min(end, cursor + page_limit - 1)
                    units.append(PageUnit(index, photo, cursor, chunk_end, total))
                    cursor = chunk_end + 1
            else:
                units.append(PageUnit(index, photo, start, end, total))
        return units

    @staticmethod
    def _pack_units(units, max_pages=None, max_units=None):
        """按页段贪心装册：页数或份数先到上限就换下一个"""
        groups = []
        current = []
        current_pages = 0
        for unit in units:
            if current:
                over_pages = max_pages is not None and current_pages + unit.pages > max_pages
                over_units = (
                    max_units is not None
                    and len({item.index for item in current}) >= max_units
                )
                if over_pages or over_units:
                    groups.append(current)
                    current = []
                    current_pages = 0
            current.append(unit)
            current_pages += unit.pages
        if current:
            groups.append(current)
        return groups

    def _normalize_selected_ranges(self, selected, album_dir):
        """下载完成后按实际图片数夹取页码，返回 (新选择, 提示)"""
        result = []
        notes = []
        for index, photo, page_range in selected:
            if not page_range:
                result.append((index, photo, None))
                continue
            total = len(self._chapter_image_paths(photo, album_dir))
            start, end = page_range
            if total <= 0:
                result.append((index, photo, page_range))
                continue
            if start > total:
                notes.append(f"第{index}章只有{total}页，页码 {start} 超出范围，已忽略该章")
                continue
            if end is not None and end > total:
                notes.append(f"第{index}章只有{total}页，结束页已调整为{total}")
                end = total
            if end is not None and end < start:
                start, end = end, start
            result.append((index, photo, (start, end)))
        return result, notes

    def _plan_pdf_groups(self, selected, album_dir=None):
        """规划 PDF：整章优先，仅单章超过页数上限时按页切分"""
        units = self._expand_page_units(selected, album_dir, self.pdf_page_limit)
        return self._pack_units(units, max_pages=self.pdf_page_limit)

    def _plan_zip_groups(self, selected, album_dir=None):
        """规划 ZIP：最多 zip_group_size 章，且不超过 zip_page_limit 页"""
        units = self._expand_page_units(selected, album_dir, self.zip_page_limit)
        return self._pack_units(
            units,
            max_pages=self.zip_page_limit or None,
            max_units=self.zip_group_size,
        )

    def _resume_expr(self, groups):
        """把剩余分组压缩成 -c 表达式，页段从哪页开始就写哪页

        同一章节的多个不连续页段必须分别输出，不能把中间未下载的页
        合并进来。只有重叠或相邻的页段才会合并。
        """
        info = {}
        order = []
        for group in groups:
            for unit in group:
                if unit.index not in info:
                    info[unit.index] = {"ranges": [], "total": unit.total}
                    order.append(unit.index)
                info[unit.index]["ranges"].append((unit.start, unit.end))

        parts = []
        whole = []

        def flush_whole():
            if whole:
                parts.append(self._format_index_ranges(whole))
                whole.clear()

        for index in order:
            total = info[index]["total"]
            ranges = self._merge_page_ranges(info[index]["ranges"])
            if any(start <= 1 and end is not None and end >= total
                   for start, end in ranges):
                whole.append(index)
                continue
            flush_whole()
            for start, end in ranges:
                end_text = "" if end is None or end >= total else f"{end:03d}"
                parts.append(f"{index}:{start:03d}-{end_text}")
        flush_whole()
        return ",".join(part for part in parts if part)

    @staticmethod
    def _format_index_ranges(indexes):
        """把 [1,2,3,5,7,8] 压缩成 1-3,5,7-8"""
        values = sorted(set(int(x) for x in indexes))
        if not values:
            return ""
        parts = []
        start = previous = values[0]
        for value in values[1:]:
            if value == previous + 1:
                previous = value
            else:
                parts.append(str(start) if start == previous else f"{start}-{previous}")
                start = previous = value
        parts.append(str(start) if start == previous else f"{start}-{previous}")
        return ",".join(parts)

    def _chapter_dir(self, photo, album_dir=None):
        """定位章节图片目录"""
        candidates = []
        try:
            candidates.append(Path(self.option.decide_image_save_dir(photo, ensure_exists=False)))
        except Exception:
            pass
        save_path = getattr(photo, "save_path", "")
        if save_path:
            candidates.append(Path(save_path))

        for folder in candidates:
            try:
                if folder.is_dir() and self._album_dir_has_images(folder):
                    return folder
            except Exception:
                continue

        # 兜底：在漫画目录下按章节ID/章节名匹配
        if album_dir is not None:
            name = getattr(photo, "name", "") or ""
            photo_id = str(getattr(photo, "photo_id", "") or "")
            try:
                subs = [sub for sub in album_dir.iterdir() if sub.is_dir()]
                # 1) 新版精确 P{id}，旧版 P{id}_标题 兼容
                if photo_id:
                    exact = album_dir / f"P{photo_id}"
                    if exact.is_dir() and self._album_dir_has_images(exact):
                        return exact
                    for sub in subs:
                        if sub.name.startswith(f"P{photo_id}_") and self._album_dir_has_images(sub):
                            return sub
                # 2) 标题完全匹配
                if name:
                    for sub in subs:
                        if sub.name == name and self._album_dir_has_images(sub):
                            return sub
                    # 3) 最后才允许包含匹配（旧缓存标题可能带前后缀）
                    for sub in subs:
                        if name in sub.name and self._album_dir_has_images(sub):
                            return sub
            except Exception:
                pass
        return None

    @staticmethod
    def _chapter_images(chapter_dir: Path):
        files = []
        for pattern in ("*.jpg", "*.jpeg", "*.png", "*.webp", "*.gif"):
            files.extend(chapter_dir.glob(pattern))
        return sorted(files, key=lambda p: p.name)

    def _output_fresh(self, output: Path, source_dirs):
        """输出文件存在且不比源图片/元数据旧时直接复用"""
        if not output.exists():
            return False
        try:
            out_mtime = output.stat().st_mtime
            for source in source_dirs:
                path = Path(source)
                files = [path] if path.is_file() else path.rglob("*")
                for item in files:
                    if item.is_file() and item.stat().st_mtime > out_mtime:
                        return False
            return True
        except Exception:
            return False

    def _find_cover_font(self, size):
        from PIL import ImageFont
        windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
        candidates = [
            Path(windir) / "Fonts" / "msyh.ttc",
            Path(windir) / "Fonts" / "msyhbd.ttc",
            Path(windir) / "Fonts" / "simhei.ttf",
            Path(windir) / "Fonts" / "simsun.ttc",
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        ]
        for font_path in candidates:
            try:
                if font_path.is_file():
                    return ImageFont.truetype(str(font_path), size)
            except Exception:
                continue
        return None

    def _make_cover_image(self, text, cover_path):
        """用系统 CJK 字体生成 PDF 首页封面；找不到字体时放弃封面"""
        try:
            from PIL import Image, ImageDraw
            title_font = self._find_cover_font(54)
            body_font = self._find_cover_font(30)
            if title_font is None or body_font is None:
                logger.warning("未找到中文字体，跳过 PDF 标题封面")
                return None

            width, height = 1240, 1754
            margin = 80
            image = Image.new("RGB", (width, height), "white")
            draw = ImageDraw.Draw(image)
            max_width = width - margin * 2

            def wrap_line(line, font):
                if not line:
                    return [""]
                result = []
                current = ""
                for char in line:
                    if draw.textlength(current + char, font=font) <= max_width:
                        current += char
                    else:
                        result.append(current)
                        current = char
                result.append(current)
                return result

            rendered = []
            for line_index, line in enumerate(str(text).splitlines()):
                font = title_font if line_index == 0 else body_font
                for wrapped in wrap_line(line, font):
                    rendered.append((wrapped, font))

            y = margin
            for line, font in rendered:
                if y + font.size > height - margin:
                    break
                draw.text((margin, y), line, font=font, fill=(25, 25, 25))
                y += font.size + (18 if font is title_font else 12)
                if font is title_font and y < height:
                    draw.line((margin, y - 8, width - margin, y - 8), fill=(180, 180, 180), width=2)
                    y += 12

            image.save(cover_path, "JPEG", quality=95)
            return cover_path
        except Exception as e:
            logger.warning(f"生成 PDF 封面失败: {e}")
            return None

    def _build_pdf(self, image_paths, pdf_path: Path, source_dirs=None,
                   metadata=None, cover_text=None):
        """把图片按顺序合成一个 PDF。

        优先使用 img2pdf（不把所有图片解码进内存，能显著降低大文件崩溃概率），
        不可用或遇到不支持的图片格式时回退到 reportlab/Pillow。
        cover_text 不为空时，在首页插入一张标题封面（不计入图片页上限）。
        """
        if source_dirs and self._output_fresh(pdf_path, source_dirs):
            return pdf_path
        if not image_paths:
            return None

        cover_path = None
        if cover_text:
            cover_path = pdf_path.with_name(pdf_path.stem + "_cover.jpg")
            if not self._make_cover_image(cover_text, cover_path):
                cover_path = None
        if cover_path is not None:
            image_paths = [cover_path] + list(image_paths)
        metadata = metadata or {}

        try:
            try:
                import img2pdf
            except ImportError:
                img2pdf = None
            if img2pdf is not None:
                try:
                    kwargs = {
                        key: str(metadata[key])
                        for key in ("title", "author", "subject", "keywords")
                        if metadata.get(key)
                    }
                    # 元数据写入失败时不再“无元数据重试”，直接交给
                    # reportlab 分支，优先保证 PDF 标题属性存在。
                    pdf_bytes = img2pdf.convert(
                        [str(path) for path in image_paths], **kwargs
                    )
                    pdf_path.write_bytes(pdf_bytes)
                    return pdf_path
                except Exception as e:
                    logger.warning(f"img2pdf 合成失败，尝试 reportlab: {e}")

            # reportlab 逐页写入，不把所有图片同时解码到内存里
            try:
                from PIL import Image
                from reportlab.pdfgen import canvas
                from reportlab.lib.utils import ImageReader

                pdf_canvas = canvas.Canvas(str(pdf_path))
                for setter, key in (
                    ("setTitle", "title"),
                    ("setAuthor", "author"),
                    ("setSubject", "subject"),
                    ("setKeywords", "keywords"),
                ):
                    try:
                        if metadata.get(key):
                            getattr(pdf_canvas, setter)(str(metadata[key]))
                    except Exception:
                        pass
                wrote_page = False
                for image_path in image_paths:
                    try:
                        with Image.open(image_path) as source:
                            width, height = source.size
                            if source.mode not in ("RGB", "L"):
                                source = source.convert("RGB")
                            buffer = io.BytesIO()
                            source.save(buffer, format="JPEG", quality=95)
                            buffer.seek(0)
                        # 按 100dpi 换算成 PDF 点，避免页面尺寸过大
                        page_width = width * 0.72
                        page_height = height * 0.72
                        pdf_canvas.setPageSize((page_width, page_height))
                        pdf_canvas.drawImage(
                            ImageReader(buffer), 0, 0,
                            width=page_width, height=page_height,
                        )
                        pdf_canvas.showPage()
                        wrote_page = True
                        buffer.close()
                    except Exception:
                        continue
                if wrote_page:
                    pdf_canvas.save()
                    return pdf_path
                pdf_canvas.save()
            except Exception as e:
                logger.warning(f"reportlab 合成失败，回退 Pillow: {e}")

            from PIL import Image

            images = []
            try:
                for image_path in image_paths:
                    try:
                        images.append(Image.open(image_path).convert("RGB"))
                    except Exception:
                        continue
                if not images:
                    return None
                images[0].save(
                    pdf_path,
                    save_all=True,
                    append_images=images[1:],
                    resolution=100.0,
                )
                return pdf_path
            finally:
                for image in images:
                    try:
                        image.close()
                    except Exception:
                        pass
        finally:
            if cover_path is not None:
                try:
                    cover_path.unlink(missing_ok=True)
                except Exception:
                    pass

    def _build_zip(self, entries, zip_path: Path, source_dirs=None, info_text=None):
        """按页段打包 ZIP。

        entries 为 [(图片列表, 压缩包内目录名或 None)]；目录名为 None 时图片放在根目录。
        info_text 会作为根目录下的“漫画信息.txt”写入。
        """
        if source_dirs and self._output_fresh(zip_path, source_dirs):
            return zip_path
        if not any(images for images, _ in entries):
            return None
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for images, arc_dir in entries:
                for image in images:
                    arc_name = f"{arc_dir}/{image.name}" if arc_dir else image.name
                    zipf.write(image, arc_name)
            if info_text:
                zipf.writestr("漫画信息.txt", str(info_text).encode("utf-8"))
        return zip_path

    def _zip_chapter_folder(self, photo, index, folder=None, album_dir=None):
        """ZIP 内章节目录名：章节标题_第XXX章（章节号放在最后，便于排序）"""
        title = self._chapter_display_title(photo, index, folder, album_dir)
        title = self._safe_filename(title or f"第{index}章", 60)
        return f"{title}_第{index:03d}章"

    def _chapter_file_stem(self, photo, index, folder=None, album_title="", album_dir=None):
        """单章文件：JM章节ID_章节标题（不再拼接漫画标题）"""
        title = self._chapter_display_title(photo, index, folder, album_dir)
        title = self._safe_filename(title or f"第{index}章", 40)
        return f"JM{getattr(photo, 'photo_id', index)}_{title}"

    def _group_file_stem(self, album_id, album_title, indexes):
        """多章合并文件：JM漫画ID_章节号列表（不再拼接漫画标题）。

        连续章节用 031-035；非连续章节用 031,035,040，避免把实际
        只含三章的文件标成 031-040。
        """
        values = sorted({int(x) for x in indexes})
        index_text = self._format_index_token(values)
        return f"JM{album_id}_{index_text}"

    def _format_index_token(self, indexes):
        """把章节号格式化为 031-035 / 031,035,040"""
        parts = []
        for token in self._format_index_ranges(indexes).split(","):
            if not token:
                continue
            if "-" in token:
                start, end = token.split("-", 1)
                parts.append(f"{int(start):03d}-{int(end):03d}")
            else:
                parts.append(f"{int(token):03d}")
        return ",".join(parts) or "000"

    def _file_stem_for_entries(self, entries, album_id, album_title, album_dir=None):
        """按页段生成文件名；页段带 _P起始-结束，多个页段分别标注章节号"""
        if len(entries) == 1:
            unit, folder, _ = entries[0]
            stem = self._chapter_file_stem(
                unit.photo, unit.index, folder, album_title, album_dir
            )
            if unit.partial:
                stem += f"_P{unit.start:03d}-{unit.end:03d}"
            return stem

        indexes = {unit.index for unit, _folder, _images in entries}
        if len(indexes) == 1:
            # 同一章节的多个不连续页段仍属于单章，继续使用章节ID+标题，
            # 每个页段单独追加 _P起始-结束，避免退化成漫画ID文件名。
            unit, folder, _ = entries[0]
            stem = self._chapter_file_stem(
                unit.photo, unit.index, folder, album_title, album_dir
            )
            for unit, _folder, _images in entries:
                if unit.partial:
                    stem += f"_P{unit.start:03d}-{unit.end:03d}"
            return stem

        stem = self._group_file_stem(
            album_id, album_title, [unit.index for unit, _folder, _images in entries]
        )
        partials = [unit for unit, _folder, _images in entries if unit.partial]
        if len(partials) == 1:
            unit = partials[0]
            stem += f"_P{unit.start:03d}-{unit.end:03d}"
        else:
            # 手动组合多个页段时逐个标注，避免不同页码组合生成同名文件
            for unit in partials:
                stem += f"_{unit.index:03d}P{unit.start:03d}-{unit.end:03d}"
        return stem

    def _zip_arc_dir(self, unit, folder, multi, album_dir=None):
        """ZIP 内章节目录名；单章直接放根目录"""
        if not multi:
            return None
        arc_dir = self._zip_chapter_folder(unit.photo, unit.index, folder, album_dir)
        if unit.partial:
            arc_dir += f"_P{unit.start:03d}-{unit.end:03d}"
        return arc_dir

    @staticmethod
    def _progress_bar(pct, width=10):
        pct = max(0, min(100, int(pct)))
        filled = int(round(pct * width / 100))
        return "█" * filled + "░" * (width - filled)

    def _progress_message(self, prog, pct):
        total = max(0, int(prog.get("total") or 0))
        done = max(0, int(prog.get("done") or 0))
        chapter = ""
        photo_total = max(0, int(prog.get("photo_total") or 0))
        photo_no = max(0, int(prog.get("current_photo") or 0))
        if photo_total > 0 and photo_no > 0:
            photo_no = min(photo_no, photo_total)
            chapter = f" 第{photo_no}/{photo_total}章"
        return (
            f"⏳ 下载进度：{pct}% "
            f"[{self._progress_bar(pct)}] ({done}/{total}张){chapter}"
        )

    async def _download_album_progress(self, event, album_id, holder, chapters=None):
        """下载整本并推送进度，结果写入 holder['dir'] / holder['duration']。

        - 只有所有章节都已有图片时才跳过下载，避免半成品缓存产出残缺 PDF；
        - 图片/CDN 瞬时失败时自动重试，已下载图片会复用缓存；
        - 重试后仍有失败时，允许发送已完成章节，并由上层报告部分失败。
        """
        folder = self._find_album_dir(album_id)
        if folder is not None and self._album_cache_complete(folder, chapters):
            holder["dir"] = folder
            holder["duration"] = 0.0
            yield self._plain(event, f"📦 JM{album_id} 已有缓存，跳过下载")
            return
        if folder is not None:
            yield self._plain(event,
                f"📦 JM{album_id} 缓存不完整，将只补下缺失章节/图片..."
            )

        attempts = 3
        for attempt in range(1, attempts + 1):
            ProgressDownloader._progress.pop(str(album_id), None)
            task = asyncio.create_task(
                jmcomic.download_album_async(album_id, self.option, downloader=ProgressDownloader)
            )
            progress_step = int(getattr(self, "progress_step", 10) or 0)
            last_pct = -1
            next_step = progress_step
            last_sent_at = time.monotonic()
            try:
                while not task.done():
                    await asyncio.sleep(3)
                    if progress_step <= 0:
                        continue
                    prog = ProgressDownloader._progress.get(str(album_id))
                    if not prog or prog.get("finished"):
                        continue
                    total = max(0, int(prog.get("total") or 0))
                    done = max(0, int(prog.get("done") or 0))
                    if total <= 0:
                        continue
                    pct = min(100, max(0, int(done * 100 / total)))
                    now = time.monotonic()
                    step_due = pct >= next_step and pct < 100 and pct != last_pct
                    heartbeat_due = (now - last_sent_at) >= 60 and pct < 100
                    if not (step_due or heartbeat_due):
                        continue
                    message = self._progress_message(prog, pct)
                    logger.info(message)
                    yield self._plain(event, message)
                    last_pct = pct
                    last_sent_at = now
                    next_step = ((pct // progress_step) + 1) * progress_step
            except asyncio.CancelledError:
                # 用户执行 /jmstop 时，把底层整本下载任务一起取消
                if not task.done():
                    task.cancel()
                try:
                    await task
                except BaseException:
                    pass
                raise

            try:
                result = task.result()
            except (PartialDownloadFailedException, RequestRetryAllFailException) as e:
                folder = self._find_album_dir(album_id)
                if attempt < attempts:
                    yield self._plain(event,
                        f"⚠️ 第{attempt}次下载有图片失败（{type(e).__name__}），"
                        "正在重试，已下载图片会复用缓存..."
                    )
                    await asyncio.sleep(3)
                    continue
                if folder is not None and self._album_dir_has_images(folder):
                    holder["dir"] = folder
                    holder["duration"] = 0.0
                    holder["partial"] = True
                    yield self._plain(event,
                        "⚠️ 重试后仍有图片下载失败，先发送已完成章节；"
                        "缺失图片已保留缓存，稍后重跑命令可继续补齐。"
                    )
                    return
                raise

            folder = self._find_album_dir(album_id)
            holder["dir"] = folder
            holder["duration"] = float(getattr(result, "duration", 0) or 0)
            if folder is None:
                yield self._plain(event, "⚠️ 下载完成但找不到漫画目录")
            elif chapters and not self._album_cache_complete(folder, chapters):
                holder["partial"] = True
                yield self._plain(event,
                    "⚠️ 下载结束但仍有章节缺失，将先发送已完成章节；"
                    "稍后重跑命令可继续补齐。"
                )
            return

    async def _ensure_photo_downloaded(self, photo, album_dir=None):
        """确保单章已下载，返回 (目录, 耗时)"""
        folder = self._chapter_dir(photo, album_dir)
        if folder is not None:
            return folder, 0.0

        try:
            result = await jmcomic.download_photo_async(photo.photo_id, self.option)
        except (PartialDownloadFailedException, RequestRetryAllFailException) as e:
            folder = self._chapter_dir(photo, album_dir)
            if folder is not None and self._album_dir_has_images(folder):
                logger.warning(f"第{getattr(photo, 'photo_id', '')}章部分图片失败，先使用已下载图片: {e}")
                return folder, 0.0
            raise
        detail = getattr(result, "detail", photo)
        if getattr(detail, "from_album", None) is None and getattr(photo, "from_album", None) is not None:
            detail.from_album = photo.from_album
        if not getattr(photo, "name", "") and getattr(detail, "name", ""):
            photo.name = detail.name
        folder = self._chapter_dir(detail, album_dir) or self._chapter_dir(photo, album_dir)
        return folder, float(getattr(result, "duration", 0) or 0)

    def _tease_for_count(self, count):
        mapping = {
            3: ["兄弟，今天已经下3本了，悠着点"],
            5: ["🚨 5本了！再看下去要樯橹灰飞烟灭了", "🚨 提醒：5本了，身体要紧啊兄弟"],
            7: ["差不多得了，出去看看太阳吧，别一直盯着屏幕"],
            10: ["🚨 警告：10本！你这频率比我上班打卡都勤", "🚨 10本了！再看下去腰都要坐坏了"],
            15: ["你这下载量，网站管理员都要注意到你了"],
            20: ["🚨 严重警告：20本！JM官方都要给你发终身VIP了", "🚨 20本了！你这是准备把整个网站搬回家？"],
            30: ["你这用量，键盘都要被你磨包浆了"],
            50: ["🚨 50本了兄弟！建议今天到此为止，明天再看"],
        }
        return mapping.get(int(count), [])

    @staticmethod
    def _add_sent_ranges(sent_ranges, photo_id, start, end):
        """记录本次批量已经发送的章节页段，并合并相邻/重叠区间"""
        key = str(photo_id)
        ranges = sent_ranges.setdefault(key, [])
        ranges.append((int(start), int(end)))
        ranges.sort()
        merged = []
        for item_start, item_end in ranges:
            if merged and item_start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], item_end))
            else:
                merged.append((item_start, item_end))
        sent_ranges[key] = merged

    @staticmethod
    def _range_covered(sent_ranges, photo_id, start, end):
        for item_start, item_end in sent_ranges.get(str(photo_id), []):
            if item_start <= start and item_end >= end:
                return True
        return False

    def _filter_duplicate_selections(self, selected, album_dir, sent_ranges):
        """过滤本批次中已经发送过的章节/页段，避免批量目标重复发同一文件"""
        result = []
        skipped = []
        for index, photo, page_range in selected:
            total = len(self._chapter_image_paths(photo, album_dir))
            if total <= 0:
                result.append((index, photo, page_range))
                continue
            start = int(page_range[0]) if page_range else 1
            end = int(page_range[1]) if page_range and page_range[1] is not None else total
            if self._range_covered(sent_ranges, getattr(photo, "photo_id", index), start, end):
                skipped.append(index)
                continue
            result.append((index, photo, page_range))
        return result, skipped

    async def _run_download(self, event, target_str, output_format,
                            chapter_expr=None):
        """统一下载入口：PDF/ZIP、单本/批量、漫画ID/章节ID、QQ文件限额"""
        raw_targets = [x.strip() for x in str(target_str).split(",") if x.strip()]
        if not raw_targets:
            yield self._plain(event, "❌ 未解析到有效ID")
            return

        targets = []
        seen = set()
        for raw in raw_targets:
            prefix, target_id = self._normalize_target(raw)
            key = f"{prefix}:{target_id}" if prefix else target_id
            if key in seen:
                continue
            seen.add(key)
            targets.append((raw, prefix, target_id))

        if not targets:
            yield self._plain(event, "❌ 未解析到有效ID")
            return

        fmt_label = "PDF" if output_format == "pdf" else "ZIP"

        try:
            client = self._new_logged_client()
        except Exception as e:
            yield self._plain(event, self._handle_jm_exception(e, "登录"))
            return

        success = 0
        failed = []
        total_files = 0
        total_bytes = 0
        quota_stopped = False
        sent_ranges = {}

        for target_index, (raw, prefix, target_id) in enumerate(targets):
            target_label = f"[{target_index + 1}/{len(targets)}] JM{target_id}"
            yield self._plain(
                event,
                f"⬇️ 开始下载 JM{target_id}，📦 共 {len(targets)} 个目标，输出格式：{fmt_label}。",
            )
            try:
                target = await self._resolve_target(client, raw)
            except Exception as e:
                failed.append(raw)
                yield self._plain(event,
                    f"❌ {target_label} 识别失败：{self._handle_jm_exception(e, '识别')}"
                )
                continue

            kind = target["kind"]
            if kind == "photo":
                album = target.get("album")
                album_id = getattr(album, "album_id", "") if album is not None else ""
                note = f"（所属漫画 JM{album_id}）" if album_id else ""
                yield self._plain(event, f"ℹ️ {target_label} 是章节ID{note}，按单章处理")

            chapters = target["chapters"]
            if not chapters:
                failed.append(raw)
                yield self._plain(event, f"⚠️ {target_label} 没有章节")
                continue

            if kind == "album" and chapter_expr:
                selected, invalid, notes = self._select_chapters(chapters, chapter_expr)
                if notes:
                    yield self._plain(event, "⚠️ 章节提示：" + "；".join(notes))
                if invalid:
                    yield self._plain(event, f"⚠️ 忽略无效章节/页段序号：{', '.join(invalid)}")
                if not selected:
                    failed.append(raw)
                    yield self._plain(event, f"⚠️ {target_label} 没有选中任何章节")
                    continue
            elif kind == "album":
                selected = [(i + 1, ch, None) for i, ch in enumerate(chapters)]
            else:
                if chapter_expr and (":" in chapter_expr or "：" in chapter_expr):
                    selected, invalid, notes = self._select_chapters(chapters, chapter_expr)
                    if notes:
                        yield self._plain(event, "⚠️ 页码提示：" + "；".join(notes))
                    if invalid:
                        yield self._plain(event,
                            f"⚠️ 忽略无效页段：{', '.join(invalid)}"
                        )
                    if not selected:
                        failed.append(raw)
                        yield self._plain(event, f"⚠️ {target_label} 页码段无效")
                        continue
                else:
                    selected = [(1, chapters[0], None)]
                    if chapter_expr:
                        yield self._plain(
                            event,
                            "ℹ️ 章节ID目标只支持页段参数（如 -c 1:201-），已忽略章节参数",
                        )

            album = target.get("album")
            album_id = str(
                getattr(album, "album_id", "")
                or getattr(target.get("photo"), "album_id", "")
                or (target_id if kind == "album" else "")
            )
            album_title = getattr(album, "name", "") if album is not None else ""
            album_dir = None
            # 选章/单章先找已有漫画目录，便于复用已有图片缓存
            if album_id and not (kind == "album" and not chapter_expr):
                album_dir = self._find_album_dir(album_id)

            # 整本下载走 download_album_async；选章/单章逐章下载
            if kind == "album" and not chapter_expr:
                holder = {}
                try:
                    async for message in self._download_album_progress(
                        event, target_id, holder, chapters=chapters
                    ):
                        yield message
                except Exception as e:
                    failed.append(raw)
                    yield self._plain(event,
                        f"❌ {target_label} 下载失败：{self._handle_jm_exception(e, '下载')}"
                    )
                    continue
                album_dir = holder.get("dir")
                if album_dir is None:
                    failed.append(raw)
                    continue
            else:
                # 同一章节可能有多个不连续页段，下载时按章节去重，
                # 避免重复请求同一章、并把刚下载的章误报成“已有缓存”。
                download_items = []
                seen_photos = set()
                for original_index, photo, _page_range in selected:
                    photo_key = str(getattr(photo, "photo_id", original_index))
                    if photo_key in seen_photos:
                        continue
                    seen_photos.add(photo_key)
                    download_items.append((original_index, photo))

                cached_indexes = []
                for position, (original_index, photo) in enumerate(download_items):
                    if self._chapter_dir(photo, album_dir) is not None:
                        cached_indexes.append(original_index)
                        continue
                    yield self._plain(event,
                        f"⬇️ {target_label} 下载第{original_index}章"
                        f"（{position + 1}/{len(download_items)}）..."
                    )
                    try:
                        await self._ensure_photo_downloaded(photo, album_dir)
                    except Exception as e:
                        yield self._plain(event,
                            f"❌ 第{original_index}章下载失败：{e}"
                        )
                if cached_indexes:
                    yield self._plain(event,
                        f"📦 {target_label} 第{self._format_index_ranges(cached_indexes)}章"
                        "已有缓存，跳过下载"
                    )

            if album_id and album_dir is None:
                album_dir = self._find_album_dir(album_id)
            if album_dir is not None:
                self._save_target_metadata(
                    album,
                    album_id,
                    chapters,
                    album_dir,
                    selected_photos=[photo for _index, photo, _range in selected],
                )
                album_title = self._album_display_title(album, album_dir) or album_title

            # 下载完成后按实际图片数夹取页码，再规划输出文件
            selected, range_notes = self._normalize_selected_ranges(selected, album_dir)
            if range_notes:
                yield self._plain(event, "⚠️ 页码提示：" + "；".join(range_notes))
            selected, duplicate_indexes = self._filter_duplicate_selections(
                selected, album_dir, sent_ranges
            )
            if duplicate_indexes:
                yield self._plain(
                    event,
                    f"ℹ️ {target_label} 第{self._format_index_ranges(duplicate_indexes)}章"
                    "已在本批次发送过，跳过重复文件"
                )
            if not selected:
                if duplicate_indexes:
                    success += 1
                else:
                    failed.append(raw)
                    yield self._plain(event, f"⚠️ {target_label} 没有可用的章节页段")
                continue

            # PDF：整章优先，单章超限按页切；ZIP：章数 + 页数双上限
            if output_format == "pdf":
                groups = self._plan_pdf_groups(selected, album_dir)
            else:
                groups = self._plan_zip_groups(selected, album_dir)

            target_sizes = []
            target_sent = 0
            target_failed = 0
            pdf_cover_pending = output_format == "pdf"
            if kind == "photo":
                target_title = self._chapter_display_title(
                    target.get("photo"), 1, None, album_dir
                ) or target_id
            else:
                target_title = album_title or target_id

            for group_index, group in enumerate(groups):
                quota = self._file_quota()
                if self._quota_reached(quota):
                    message = self._quota_block_text(len(groups) - group_index)
                    remaining_expr = self._resume_expr(groups[group_index:])
                    command = "/jm" if output_format == "pdf" else "/jmzip"
                    resume_target = f"p{target_id}" if kind == "photo" else target_id
                    if remaining_expr:
                        message += f"\n稍后继续：{command} {resume_target} -c {remaining_expr}"
                    yield self._plain(event, message)
                    if target_index + 1 < len(targets):
                        yield self._plain(event,
                            "未处理的目标：" + ", ".join(x[0] for x in targets[target_index + 1:])
                        )
                    quota_stopped = True
                    break

                chapter_entries = []
                for unit in group:
                    folder = self._chapter_dir(unit.photo, album_dir)
                    if folder is None:
                        try:
                            folder, _ = await self._ensure_photo_downloaded(unit.photo, album_dir)
                        except Exception as e:
                            logger.warning(f"第{unit.index}章补下载失败: {e}")
                            folder = None
                    if folder is None:
                        target_failed += 1
                        continue
                    images = self._chapter_images(folder)
                    if not images:
                        target_failed += 1
                        continue
                    # 规划后图片数可能变化，按实际图片再夹一次
                    start = max(1, min(unit.start, len(images)))
                    end = min(unit.end, len(images))
                    if end < start:
                        target_failed += 1
                        continue
                    unit = unit._replace(start=start, end=end, total=len(images))
                    chapter_entries.append((unit, folder, images[start - 1:end]))

                if not chapter_entries:
                    yield self._plain(event,
                        f"⚠️ {target_label} 第{group[0].index}章没有图片"
                    )
                    continue

                page_count = sum(len(images) for _, _, images in chapter_entries)
                yield self._plain(
                    event, f"开始打包{output_format.upper()}，共 {page_count} 页..."
                )

                source_dirs = [folder for _, folder, _ in chapter_entries]
                album_metadata_path = self._metadata_file(album_dir)
                if album_metadata_path is not None and album_metadata_path.is_file():
                    source_dirs = source_dirs + [album_metadata_path]
                image_paths = [
                    image for _, _, images in chapter_entries for image in images
                ]
                info_text = self._album_info_text(
                    album, album_id, album_dir, chapter_entries
                )
                cover_text = None
                pdf_metadata = None
                if output_format == "pdf":
                    pdf_metadata = self._pdf_metadata(
                        album, album_id, album_dir, chapter_entries
                    )
                    if pdf_cover_pending:
                        cover_text = self._album_cover_text(
                            album, album_id, album_dir, chapter_entries
                        )
                try:
                    stem = self._file_stem_for_entries(
                        chapter_entries, album_id, album_title, album_dir
                    )
                    output_path = self.zip_base_dir / f"{stem}.{output_format}"
                    if output_format == "pdf":
                        built = await asyncio.to_thread(
                            self._build_pdf, image_paths, output_path, source_dirs,
                            pdf_metadata, cover_text,
                        )
                    else:
                        multi = len(chapter_entries) > 1
                        zip_entries = [
                            (images, self._zip_arc_dir(unit, folder, multi, album_dir))
                            for unit, folder, images in chapter_entries
                        ]
                        built = await asyncio.to_thread(
                            self._build_zip, zip_entries, output_path, source_dirs,
                            info_text,
                        )
                except Exception as e:
                    yield self._plain(event, f"❌ 生成文件失败：{e}")
                    target_failed += len(group)
                    continue

                if not built or not output_path.exists():
                    yield self._plain(event, "❌ 生成文件失败（没有可用图片）")
                    target_failed += len(group)
                    continue
                if output_format == "pdf":
                    pdf_cover_pending = False

                size_mb = output_path.stat().st_size / 1024 / 1024
                # 按约 2MB/s 估算 QQ 上传耗时，并计入发送间隔
                estimate_seconds = max(
                    1, math.ceil(size_mb / 2.0) + int(math.ceil(self.send_interval))
                )
                yield self._plain(
                    event,
                    f"📤 打包完成：文件 {size_mb:.1f}MB，"
                    f"预计发送耗时约 {estimate_seconds} 秒。"
                )
                self._record_file_send()
                total_files += 1
                target_sent += 1
                for unit, _folder, _images in chapter_entries:
                    self._add_sent_ranges(
                        sent_ranges, getattr(unit.photo, "photo_id", unit.index),
                        unit.start, unit.end,
                    )
                total_bytes += output_path.stat().st_size
                yield self._chain(event, [
                    Comp.Plain(f"📎 {output_path.name} ({size_mb:.1f}MB)\n"),
                    Comp.File(file=str(output_path), name=output_path.name),
                ])
                target_sizes.append(size_mb)
                output_path.unlink(missing_ok=True)
                await asyncio.sleep(self.send_interval)

            if target_sent:
                total_count = self._add_user_history(event.get_sender_id(), {
                    "id": target_id,
                    "title": target_title,
                    "time": time.strftime("%Y-%m-%d %H:%M"),
                    "size_mb": round(sum(target_sizes), 1),
                })
                success += 1
                if not quota_stopped:
                    teases = self._tease_for_count(total_count)
                    if self.tease_enabled and teases:
                        import random
                        yield self._plain(event, random.choice(teases))
            if target_failed:
                failed.append(f"{raw}(部分章节失败)" if target_sent else raw)

            if quota_stopped:
                break

        if not quota_stopped:
            yield self._plain(event,
                f"📊 下载完成：成功 {success}/{len(targets)} 个目标，"
                f"发送 {total_files} 个文件，总计 {total_bytes / 1024 / 1024:.1f}MB\n"
                f"{self._quota_status_text()}"
            )
        if failed:
            yield self._plain(event, f"⚠️ 失败目标：{', '.join(failed)}")

    def _parse_search_args(self, args):
        """
        解析搜索参数：关键词 + 可选页码 + 可选排序
        支持格式：
          /jms 关键词
          /jms 关键词 2
          /jms 关键词 mv
          /jms 关键词 2 mv
        返回：(keyword, qq_page, order_by)
        """
        keyword = args[1]
        qq_page = 1
        order_by = JmMagicConstants.ORDER_BY_LATEST  # 默认最新

        # 解析后面的参数（页码/排序）。非法页码不再静默回退到第1页。
        extra = args[2:]
        for arg in extra:
            if re.fullmatch(r"\d+", arg):
                qq_page = int(arg)
                if qq_page < 1:
                    raise ValueError("页码必须是大于 0 的整数")
            elif arg in self.order_map:
                order_by = arg
            else:
                raise ValueError(
                    f"无法识别的参数「{arg}」；页码需为正整数，"
                    "排序可选 mr/mv/mp/tf/tr/md"
                )

        return keyword, qq_page, order_by

    @staticmethod
    def _parse_positive_page(token, label="页码"):
        """严格解析正整数页码，0/负数/字母/符号都会抛出中文错误。"""
        if not re.fullmatch(r"\d+", str(token).strip()):
            raise ValueError(f"{label}必须是大于 0 的整数")
        value = int(token)
        if value < 1:
            raise ValueError(f"{label}必须是大于 0 的整数")
        return value

    async def _render_search_page(self, event, client, search_method, keyword, qq_page, order_by, title_prefix="", command_name="jms"):
        """通用搜索结果渲染：分页 + 切片 + 输出"""
        PER_QQ_PAGE = self.per_page
        PER_WEB_PAGE = 80
        QQ_PAGES_PER_WEB_PAGE = PER_WEB_PAGE // PER_QQ_PAGE

        web_page = math.ceil(qq_page / QQ_PAGES_PER_WEB_PAGE)
        offset_in_web_page = (qq_page - 1) % QQ_PAGES_PER_WEB_PAGE * PER_QQ_PAGE

        order_name = self.order_map.get(order_by, order_by)
        display_keyword = self._safe_text(keyword, self.convert_title)
        no_result_text = f"没有找到与「{display_keyword}」相关的漫画，请换个关键词试试"
        yield self._plain(event, f"🔍 {title_prefix}{display_keyword}（{order_name}，第{qq_page}页）...")

        try:
            total = None
            totals = []
            if self.dual_search:
                # 搜两次：原词 + 繁体词，合并去重，适配半简半繁标题
                import zhconv
                keyword_traditional = zhconv.convert(keyword, 'zh-tw')
                # 如果原词和繁体词相同，只搜一次
                search_keywords = [keyword] if keyword == keyword_traditional else [keyword, keyword_traditional]
                all_items = []
                for kw in search_keywords:
                    page = await asyncio.to_thread(search_method, kw, web_page, order_by)
                    page_total = getattr(page, 'total', None)
                    if page_total is not None:
                        totals.append(int(page_total))
                    all_items.extend(list(page.iter_id_title_tag()))
                if totals:
                    total = max(totals)
                # 去重
                seen = set()
                unique_items = []
                for item in all_items:
                    if item[0] not in seen:
                        seen.add(item[0])
                        unique_items.append(item)
                all_items = unique_items
            else:
                page = await asyncio.to_thread(search_method, keyword, web_page, order_by)
                total = getattr(page, 'total', None)
                all_items = list(page.iter_id_title_tag())
            start = offset_in_web_page
            end = start + PER_QQ_PAGE
            page_items = all_items[start:end]

            # 上游 API 对超大页码会 clamp 到最后一页，不能只看返回是否为空。
            total_int = None
            if total is not None:
                try:
                    total_int = int(total)
                except (TypeError, ValueError):
                    total_int = None
            if total_int is not None and total_int <= 0:
                yield self._plain(event, no_result_text)
                return
            if total_int is not None:
                max_page = math.ceil(total_int / PER_QQ_PAGE)
                if qq_page > max_page:
                    yield self._plain(event,
                        f"第{qq_page}页没有更多结果了（共{total_int}条，最多{max_page}页）"
                    )
                    return

            if not page_items:
                if total_int is not None:
                    yield self._plain(event, f"第{qq_page}页没有更多结果了")
                else:
                    yield self._plain(event, no_result_text)
                return

            results = []
            for i, (aid, item_title, tags) in enumerate(page_items):
                global_index = (qq_page - 1) * PER_QQ_PAGE + i + 1
                item_title = self._safe_text(item_title, self.convert_title)
                tags = [self._safe_text(t, self.convert_tags) for t in tags]
                safe_tags = tags[:3]
                tag_str = f" [{', '.join(safe_tags)}]" if safe_tags else ""
                results.append(f"{global_index}. [JM{aid}] {item_title}{tag_str}")

            reply = f"{title_prefix}：{display_keyword}（{order_name}）\n"
            global_start = (qq_page - 1) * PER_QQ_PAGE
            range_text = f"第{qq_page}页，第{global_start + 1}-{global_start + len(page_items)}条"
            if total_int is not None:
                range_text += f"，共{total_int}条"
            reply += range_text + "\n\n" + "\n".join(results)
            reply += f"\n\n/jmv <id> 查详情，/jm <id> 下载"

            # 下一页提示：保留当前搜索命令和排序参数。
            # 关键词若会被敏感词转换，就不把原词写进命令，避免命令本身触发违规。
            next_keyword = keyword if self._safe_text(keyword, self.convert_title) == keyword else "<关键词>"
            next_cmd = f"/{command_name} {next_keyword} {qq_page + 1}"
            if order_by != JmMagicConstants.ORDER_BY_LATEST:
                next_cmd += f" {order_by}"
            reply += f"\n下一页：{next_cmd}"

            yield self._plain(event, reply)

        except Exception as e:
            yield self._plain(event, self._handle_jm_exception(e, "搜索"))

    # ========== 工具：从消息里提取数字ID ==========
    def _extract_id(self, message_str: str) -> str:
        """从消息字符串里提取第一个连续数字，作为漫画ID"""
        import re
        match = re.search(r'\d+', message_str)
        return match.group() if match else None

    # ========== /jmv 查详情 ==========
    @filter.command("jmv")
    async def jm_view(self, event: AstrMessageEvent):
        """查看漫画详情，用法：/jmv <id> [选项]
        选项：
          -author  去掉作者
          -work    去掉作品
          -role    去掉人物
          -tag     去掉标签
          -pic     不发送封面
        示例：/jmv 123456 -role -work
        """
        if not self._check_permission(event):
            yield self._plain(event, self._permission_denied_text(event))
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield self._plain(event,
                "用法：/jmv <漫画id> [选项]\n"
                "选项：-author 去掉作者 | -work 去掉作品 | -role 去掉人物 | -tag 去掉标签\n"
                "      -pic 不发送封面\n"
                "示例：/jmv 350234 或 /jmv 350234 -role -work"
            )
            return

        raw_id = args[1].strip()
        if raw_id.lower().startswith("jm"):
            raw_id = raw_id[2:].strip()
        if raw_id.lower().startswith("a"):
            raw_id = raw_id[1:].strip()
        if not raw_id.isdigit():
            yield self._plain(event,
                f"❌ 漫画ID必须是数字：{args[1]}\n"
                "示例：/jmv 350234 -role"
            )
            return
        album_id = raw_id

        valid_options = {"-author", "-work", "-role", "-tag", "-pic"}
        unknown_options = [token for token in args[2:] if token.lower() not in valid_options]
        if unknown_options:
            yield self._plain(event,
                f"❌ 无法识别的选项：{', '.join(unknown_options)}\n"
                "可用选项：-author 不带作者 | -work 不带作品 | -role 不带角色 | "
                "-tag 不带标签 | -pic 不发封面"
            )
            return
        options = {token.lower() for token in args[2:]}

        option_labels = []
        for opt, label in (
            ("-author", "不带作者"),
            ("-work", "不带作品"),
            ("-role", "不带角色"),
            ("-tag", "不带标签"),
            ("-pic", "不发封面"),
        ):
            if opt in options:
                option_labels.append(label)
        option_suffix = f"（{'、'.join(option_labels)}）" if option_labels else ""
        yield self._plain(event, f"🔍 查询 JM{album_id} ...{option_suffix}")

        try:
            client = self._new_logged_client()
            album = await asyncio.to_thread(client.get_album_detail, album_id)

            if self._looks_like_chapter_album(album):
                parent_id = await self._resolve_chapter_album_parent(client, album, album_id)
                if parent_id:
                    parent_line = (
                        f"该章节所属漫画：JM{parent_id}\n"
                        f"请用 /jm {album_id} 下载该章节，或用 /jmv {parent_id} 查看漫画。"
                    )
                else:
                    parent_line = (
                        f"请用 /jm {album_id} 下载该章节；"
                        "章节所属漫画ID未能识别，可先执行 /jm 后查看缓存目录。"
                    )
                yield self._plain(event,
                    f"❌ JM{album_id} 看起来是章节ID，不是漫画ID。\n"
                    f"{parent_line}"
                )
                return

            chapters = list(album) if hasattr(album, '__iter__') else []
            authors = getattr(album, 'authors', [])
            works = getattr(album, 'works', [])
            actors = getattr(album, 'actors', [])
            tags = getattr(album, 'tags', [])
            page_count = getattr(album, 'page_count', 0) or 0
            comment_count = getattr(album, 'comment_count', 0) or 0
            pub_date = getattr(album, 'pub_date', '') or ''
            update_date = getattr(album, 'update_date', '') or ''
            likes = getattr(album, 'likes', '') or ''
            views = getattr(album, 'views', '') or ''

            # API客户端 page_count 可能为0，从章节累加计算
            if page_count == 0 and chapters:
                page_count = sum(len(getattr(p, 'page_arr', []) or []) for p in chapters)

            def join_values(values, simplify):
                if not values:
                    return "无"
                return ", ".join(self._safe_text(str(v), simplify) for v in values)

            def display_date(value):
                text = str(value or "").strip()
                return "未知" if text in ("", "0", "None") else text

            title = self._safe_text(getattr(album, 'name', ''), self.convert_title) or "未知"
            author_str = join_values(authors, self.convert_author)
            work_str = join_values(works, self.convert_title)
            actor_str = join_values(actors, self.convert_tags)
            tag_str = join_values(tags, self.convert_tags)

            sep = "━━━━━━━━━━━━━━━"
            pub_text = display_date(pub_date)
            update_text = display_date(update_date)
            if update_text == "未知" and pub_text != "未知":
                # API 不提供 update_date 时退回发布日期，避免所有漫画都显示“未知”
                update_text = pub_text
            lines = [
                sep,
                f"  📖 标题:  {title}",
                f"  🆔 ID:    JM{album_id}",
            ]
            if '-author' not in options:
                lines.append(f"  ✍️ 作者:  {author_str}")
            lines.append(sep)
            lines.append(f"  📅 发布日期:  {pub_text}")
            if update_text != "未知":
                lines.append(f"  📅 更新日期:  {update_text}")
            lines.append(f"  📄 总页数:    {page_count if page_count else '未知'}")
            lines.append(f"  👀 观看:      {views or '未知'}")
            lines.append(f"  ❤️ 点赞:      {likes or '未知'}")
            lines.append(f"  💬 评论:      {comment_count if comment_count else '未知'}")
            lines.append(sep)
            if '-tag' not in options:
                lines.append(f"  🏷️ 标签:  {tag_str}")
            if '-role' not in options:
                lines.append(f"  🎭 人物:  {actor_str}")
            if '-work' not in options:
                lines.append(f"  📚 作品:  {work_str}")
            lines.append(sep)
            lines.append(f"  📑 章节 ({len(chapters)}):")
            for i, photo in enumerate(chapters[:10]):
                photo_title = self._safe_text(getattr(photo, 'name', ''), self.convert_title) or f"第{i+1}章"
                lines.append(f"     {photo_title[:50]}  (id: {photo.photo_id})")
            if len(chapters) > 10:
                lines.append(f"     ... 共{len(chapters)}章，仅显示前10章")
            if not chapters:
                lines.append("     暂无章节")
            lines.append(sep)
            lines.append(
                f"💡 /jmlist {album_id} 章节 | /jm {album_id} PDF | /jmzip {album_id} ZIP"
            )

            reply = "\n".join(lines)
            yield self._plain(event, reply)

            # 下载封面并发送；-pic 不发送封面
            if '-pic' not in options:
                cover_path = self.data_dir / f"cover_{album_id}.jpg"
                try:
                    await asyncio.to_thread(client.download_album_cover, album_id, str(cover_path))
                    if cover_path.exists():
                        yield self._chain(event, [
                            Comp.Image(file=str(cover_path))
                        ])
                        # 发完就删，不存着
                        cover_path.unlink(missing_ok=True)
                except Exception as e:
                    logger.warning(f"封面下载失败: {e}")

        except Exception as e:
            yield self._plain(event, self._handle_jm_exception(e, "查询"))

    # ========== /jmlist 章节列表 ==========
    @filter.command("jmlist")
    async def jm_chapter_list(self, event: AstrMessageEvent):
        """查看漫画章节列表，用法：/jmlist <漫画id> [页码]
        只发送章节文本，不发送封面；封面请用 /jmv <漫画id> 查看
        """
        if not self._check_permission(event):
            yield self._plain(event, self._permission_denied_text(event))
            return

        args = event.message_str.split()
        if len(args) < 2:
            yield self._plain(event,
                "用法：/jmlist <漫画id> [页码]\n"
                "示例：/jmlist 123456\n"
                "      /jmlist 123456 2     查看第2页章节"
            )
            return

        prefix, album_id = self._normalize_target(args[1])
        if prefix == "p" or not album_id.isdigit():
            yield self._plain(event,
                f"❌ 漫画ID必须是数字：{args[1]}\n"
                "章节ID请用 /jm <章节id> 下载"
            )
            return
        if len(args) > 3:
            yield self._plain(event, "用法：/jmlist <漫画id> [页码]\n页码只能填一个")
            return
        try:
            page_num = self._parse_positive_page(args[2]) if len(args) >= 3 else 1
        except ValueError as e:
            yield self._plain(event, f"⚠️ {e}\n用法：/jmlist {album_id} [页码]")
            return

        yield self._plain(event, f"📚 获取 JM{album_id} 的章节列表...")

        try:
            client = self._new_logged_client()
            album = await asyncio.to_thread(client.get_album_detail, album_id)

            if self._looks_like_chapter_album(album):
                parent_id = await self._resolve_chapter_album_parent(client, album, album_id)
                if parent_id:
                    parent_line = (
                        f"该章节所属漫画：JM{parent_id}\n"
                        f"请用 /jm {album_id} 下载该章节，或用 /jmv {parent_id} 查看漫画。"
                    )
                else:
                    parent_line = (
                        f"请用 /jm {album_id} 下载该章节；"
                        "章节所属漫画ID未能识别，可先执行 /jm 后查看缓存目录。"
                    )
                yield self._plain(event,
                    f"❌ JM{album_id} 看起来是章节ID，不是漫画ID。\n"
                    f"{parent_line}"
                )
                return

            chapters = list(album) if hasattr(album, '__iter__') else []

            if not chapters:
                yield self._plain(event,
                    f"⚠️ JM{album_id} 没有章节列表。\n"
                    "请检查 ID 是否正确；如果是章节ID，请用 /jm <章节id> 下载。"
                )
                return

            page_size = 20
            total_pages = max(1, math.ceil(len(chapters) / page_size))
            if page_num > total_pages:
                yield self._plain(event,
                    f"⚠️ JM{album_id} 共 {len(chapters)} 章，只有 {total_pages} 页，"
                    f"已到最后一页（/jmlist {album_id} {total_pages}）"
                )
                return

            start = (page_num - 1) * page_size
            page_items = chapters[start:start + page_size]
            album_title = self._safe_text(album.name, self.convert_title)

            lines = [
                f"📚 {album_title}",
                f"🆔 JM{album_id} | 共{len(chapters)}章 | 第{page_num}/{total_pages}页",
                "━━━━━━━━━━━━━━━",
            ]
            for i, photo in enumerate(page_items):
                index = start + i + 1
                photo_title = self._safe_text(getattr(photo, 'name', ''), self.convert_title)
                if not photo_title:
                    photo_title = f"第{index}章"
                lines.append(f"{index}. [JM{photo.photo_id}] {photo_title[:50]}")

            if page_num < total_pages:
                lines.append(f"\n下一页：/jmlist {album_id} {page_num + 1}")
            lines.append(f"💡 下载单章：/jm <章节id>；下载全本：/jm {album_id}")
            text = self._output_text(event, "\n".join(lines))

            # 直接发送，便于捕获 QQ 的“消息内容违规”异常；再用日志兜底检测静默拦截
            mark = self._log_pos()
            try:
                await event.send(MessageChain(chain=[Comp.Plain(text)]))
            except Exception as send_err:
                violation = self._is_violation_error(send_err) or self._log_has_violation(mark)
                if violation:
                    yield self._plain(event,
                        "⚠️ 章节列表被 QQ 判定为消息内容违规，已拦截。\n"
                        "标题已做敏感词转拼音；可以换一页重试，或用 /jmv <漫画id> -pic 看详情。"
                    )
                else:
                    logger.warning(f"jmlist 发送失败: {send_err}")
                    yield self._plain(event, f"❌ 章节列表发送失败：{send_err}")
                return

            # 给平台发送/日志落盘留一点时间，再检查是否静默拦截
            await asyncio.sleep(1.5)
            if self._log_has_violation(mark):
                yield self._plain(event,
                    "⚠️ 日志检测到章节列表触发了“消息内容违规”，可能已被 QQ 拦截。\n"
                    "可以换一页重试，标题已做敏感词转拼音。"
                )
                return

        except Exception as e:
            yield self._plain(event, self._handle_jm_exception(e, "获取章节列表"))

    # ========== /jm 下载 PDF（/jmphoto 为兼容别名） ==========
    @filter.command("jm", alias={"jmphoto"})
    async def jm_download(self, event: AstrMessageEvent):
        """下载漫画/章节并发送 PDF，用法：/jm <id> [-c 章节]"""
        if not self._check_permission(event):
            yield self._plain(event, self._permission_denied_text(event))
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
                "  /jmv <漫画id> [选项]       查详情(含章节/标签/人物/作品)\n"
                "    选项：-author -work -role -tag -pic(不发封面)\n"
                "  /jmlist <漫画id> [页码]    查看全部章节（纯文本，不发封面）\n"
                "  /jmc <漫画id> [页码]        看评论(翻页/回复/剧透，超页自动提示)\n"
                "━━━━━━━━━━━━━━━\n"
                "⬇️ 下载：\n"
                "  /jm <漫画id>                下载整本，PDF按后台页数上限自动分册\n"
                "  /jm <章节id>                自动识别为单章，发一个PDF\n"
                "  /jm <章节id> -c 1:201-      章节ID页段续传（章节号固定写1）\n"
                "  /jm id1,id2,id3             批量下载，支持漫画/章节ID混合\n"
                "  /jm <id> -c 31              只下第31章\n"
                "  /jm <id> -c 31-40           下第31到40章（范围倒置会自动纠正并提示）\n"
                "  /jm <id> -c 31,35,40        下指定章节\n"
                "  /jm <id> -c 5:201-          从第5章第201页继续（页段续传）\n"
                "  /jm <id> -c 5:201-400       只下第5章第201-400页\n"
                "  /jmzip <漫画id>             下载整本，按后台章数自动合并ZIP\n"
                "  /jmzip <章节id>             自动识别为单章，发一个ZIP\n"
                "  /jmzip id1,id2,id3          批量下载，支持漫画/章节ID混合\n"
                "  /jmzip <id> -c 31-40        下第31到40章\n"
                "  /jmzip <id> -c 31,35,40     下指定章节\n"
                "  /jmphoto <id>               等同 /jm <id>（兼容旧命令）\n"
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
                "  /jmhis [页码]               下载历史（每页20条）\n"
                "    用法：/jmhis clear 清空历史 | /jmhis -all 查看全部成员(管理员)\n"
                "  /jmdomain                   查看可用域名\n"
                "  /jmupdate                   查看更新日志\n"
                "  /jmrand [关键词]            随机抽一个\n"
                "━━━━━━━━━━━━━━━\n"
                "📝 排序参数：mr(最新) mv(最多浏览) mp(最多页数) tf(最多收藏) tr(评分最高) md(最多评论)\n"
                "💡 搜索小提示：建议只打前几个关键词，打全容易因简繁差异漏结果\n"
                "💾 下载缓存：JM{{漫画id}}/P{{章节id}}/图片，标题存入 metadata.json，默认保留 {cache_days} 天\n"
                "📎 打包信息：ZIP 附漫画信息.txt；PDF 写入标题属性并在第一册加封面\n"
                "📄 PDF：每 {pdf_page_limit} 页一册（整章优先，单章超限按页切分）\n"
                "🗜️ ZIP：最多 {zip_group_size} 章/册，{zip_page_text}\n"
                "⚙️ 当前客户端：{client_type}（插件配置可切换 api/html）\n"
                "📦 需 jmcomic 2.7.9+（requirements 会自动安装/升级）\n"
                "⏳ 文件限额：每日{daily_limit_text}、每小时{hourly_limit_text}（0=不限）".format(
                    client_type=self.client_type.upper(),
                    cache_days=self.cache_days,
                    pdf_page_limit=self.pdf_page_limit,
                    zip_group_size=self.zip_group_size,
                    zip_page_text=self._zip_page_hint(),
                    daily_limit_text=self._limit_text(self.daily_file_limit),
                    hourly_limit_text=self._limit_text(self.hourly_file_limit),
                )
            )
            yield self._plain(event, help_text)
            return

        try:
            target, chapter_expr = self._parse_download_args(args)
        except ValueError as e:
            yield self._plain(event, f"⚠️ {e}\n用法：/jm <漫画id|章节id> [-c 章节|章节:起页-止页]")
            return
        if not target:
            yield self._plain(event, "用法：/jm <漫画id|章节id> [-c 章节|章节:起页-止页]")
            return
        async for message in self._consume_download(
            event, target, "pdf", chapter_expr
        ):
            yield message

    async def _consume_download(self, event, target, output_format, chapter_expr):
        """包装下载生成器，登记当前任务供 /jmstop 取消。"""
        task = asyncio.current_task()
        user_id = str(event.get_sender_id())
        if task is not None:
            self._download_tasks.setdefault(user_id, set()).add(task)
        try:
            async for message in self._run_download(
                event, target, output_format, chapter_expr
            ):
                yield message
        except asyncio.CancelledError:
            yield self._plain(event, "⏹️ 当前下载已停止")
        finally:
            tasks = self._download_tasks.get(user_id)
            if tasks is not None:
                tasks.discard(task)
                if not tasks:
                    self._download_tasks.pop(user_id, None)

    # ========== /jmstop 停止下载 ==========
    @filter.command("jmstop")
    async def jm_stop(self, event: AstrMessageEvent):
        """停止当前用户正在进行的下载"""
        if not self._check_permission(event):
            yield self._plain(event, self._permission_denied_text(event))
            return
        user_id = str(event.get_sender_id())
        tasks = [
            task for task in self._download_tasks.get(user_id, set())
            if not task.done()
        ]
        if not tasks:
            yield self._plain(event, "当前没有正在进行的下载")
            return
        for task in tasks:
            task.cancel()
        yield self._plain(event, f"⏹️ 已请求停止 {len(tasks)} 个下载任务")

    # ========== /jmrand 随机漫画 ==========
    @filter.command("jmrand")
    async def jm_random(self, event: AstrMessageEvent):
        """随机抽一个漫画，用法：/jmrand [标签名]
        不填标签就从日榜随机，填了就从指定标签里随机
        """
        if not self._check_permission(event):
            yield self._plain(event, self._permission_denied_text(event))
            return
        args = event.message_str.split()
        tag_name = " ".join(args[1:]) if len(args) > 1 else None

        if tag_name:
            yield self._plain(event, f"🎲 正在从「{tag_name}」标签随机抽一个...")
        else:
            yield self._plain(event, "🎲 正在从日榜随机抽一个漫画...")

        try:
            import random
            client = self._new_logged_client()

            if tag_name:
                # 全站搜索，关键词可以是标题/标签/作者
                page = await asyncio.to_thread(client.search_site, tag_name, 1)
                items = list(page.iter_id_title_tag())
                if not items:
                    yield self._plain(event,
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
                    yield self._plain(event,
                        "🎲 暂时没有抽到漫画：日榜/周榜/月榜均未返回结果。\n"
                        "请稍后再试，或使用 /jmrand 关键词 从搜索中随机抽取。"
                    )
                    return

            album_id, title, tags = random.choice(items)
            title = self._safe_text(title, self.convert_title)
            tags = [self._safe_text(t, self.convert_tags) for t in tags]
            safe_tags = tags[:5]

            # 查一下详情，拿章节数和页数
            album = await asyncio.to_thread(client.get_album_detail, album_id)
            chapters = list(album) if hasattr(album, '__iter__') else []
            page_count = getattr(album, 'page_count', '?')

            tag_str = f"\n🏷️ {', '.join(safe_tags)}" if safe_tags else ""
            yield self._plain(event,
                f"🎲 抽到了！\n\n"
                f"📖 {title}\n"
                f"🆔 JM{album_id}"
                f"{tag_str}\n"
                f"📄 {len(chapters)}章 / {page_count}页\n\n"
                f"发 /jm {album_id} 下载"
            )

        except Exception as e:
            yield self._plain(event, self._handle_jm_exception(e, "随机抽漫"))

    # ========== /jms 全站搜索 ==========
    @filter.command("jms")
    async def jm_search(self, event: AstrMessageEvent):
        """全站搜索，用法：/jms <关键词> [页码] [排序]"""
        if not self._check_permission(event):
            yield self._plain(event, self._permission_denied_text(event))
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield self._plain(event,
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
            keyword, qq_page, order_by = self._parse_search_args(args)
        except ValueError as e:
            yield self._plain(event, f"⚠️ {e}\n用法：/jms <关键词> [页码] [排序]")
            return
        client = self._new_logged_client()

        async for result in self._render_search_page(
            event, client, client.search_site, keyword, qq_page, order_by, "全站搜索", "jms"
        ):
            yield result

    # ========== /jma 按作者搜索 ==========
    @filter.command("jma")
    async def jm_search_author(self, event: AstrMessageEvent):
        """按作者搜索，用法：/jma <作者名> [页码] [排序]"""
        if not self._check_permission(event):
            yield self._plain(event, self._permission_denied_text(event))
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield self._plain(event, "用法：/jma <作者名>")
            return

        try:
            keyword, qq_page, order_by = self._parse_search_args(args)
        except ValueError as e:
            yield self._plain(event, f"⚠️ {e}\n用法：/jma <作者名> [页码] [排序]")
            return
        client = self._new_logged_client()

        async for result in self._render_search_page(
            event, client, client.search_author, keyword, qq_page, order_by, "作者搜索", "jma"
        ):
            yield result

    # ========== /jmt 按标签搜索 ==========
    @filter.command("jmt")
    async def jm_search_tag(self, event: AstrMessageEvent):
        """按标签搜索，用法：/jmt <标签名> [页码] [排序]"""
        if not self._check_permission(event):
            yield self._plain(event, self._permission_denied_text(event))
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield self._plain(event, "用法：/jmt <标签名>")
            return

        try:
            keyword, qq_page, order_by = self._parse_search_args(args)
        except ValueError as e:
            yield self._plain(event, f"⚠️ {e}\n用法：/jmt <标签名> [页码] [排序]")
            return
        client = self._new_logged_client()

        async for result in self._render_search_page(
            event, client, client.search_tag, keyword, qq_page, order_by, "标签搜索", "jmt"
        ):
            yield result

    # ========== /jmr 按角色搜索 ==========
    @filter.command("jmr")
    async def jm_search_actor(self, event: AstrMessageEvent):
        """按角色/演员搜索，用法：/jmr <角色名> [页码] [排序]"""
        if not self._check_permission(event):
            yield self._plain(event, self._permission_denied_text(event))
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield self._plain(event, "用法：/jmr <角色名>")
            return

        try:
            keyword, qq_page, order_by = self._parse_search_args(args)
        except ValueError as e:
            yield self._plain(event, f"⚠️ {e}\n用法：/jmr <角色名> [页码] [排序]")
            return
        client = self._new_logged_client()

        async for result in self._render_search_page(
            event, client, client.search_actor, keyword, qq_page, order_by, "角色搜索", "jmr"
        ):
            yield result

    # ========== /jmw 按原作搜索 ==========
    @filter.command("jmw")
    async def jm_search_work(self, event: AstrMessageEvent):
        """按原作搜索，用法：/jmw <原作名> [页码] [排序]"""
        if not self._check_permission(event):
            yield self._plain(event, self._permission_denied_text(event))
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield self._plain(event, "用法：/jmw <原作名>")
            return

        try:
            keyword, qq_page, order_by = self._parse_search_args(args)
        except ValueError as e:
            yield self._plain(event, f"⚠️ {e}\n用法：/jmw <原作名> [页码] [排序]")
            return
        client = self._new_logged_client()

        async for result in self._render_search_page(
            event, client, client.search_work, keyword, qq_page, order_by, "原作搜索", "jmw"
        ):
            yield result

    # ========== /jmzip 下载 ZIP ==========
    @filter.command("jmzip")
    async def jm_download_zip(self, event: AstrMessageEvent):
        """下载漫画/章节并发送 ZIP，用法：/jmzip <id> [-c 章节]"""
        if not self._check_permission(event):
            yield self._plain(event, self._permission_denied_text(event))
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield self._plain(event,
                "用法：/jmzip <漫画id|章节id> [-c 章节|章节:起页-止页]\n"
                "示例：/jmzip 123456\n"
                "      /jmzip 123456 -c 31-40\n"
                "      /jmzip 123456 -c 5:201-\n"
                "      /jmzip 123,456\n"
                f"说明：多章按后台配置合并 ZIP（当前最多 {self.zip_group_size} 章、"
                f"{self._zip_page_hint()}）；单章目标发一个 ZIP"
            )
            return

        try:
            target, chapter_expr = self._parse_download_args(args)
        except ValueError as e:
            yield self._plain(event, f"⚠️ {e}\n用法：/jmzip <漫画id|章节id> [-c 章节|章节:起页-止页]")
            return
        if not target:
            yield self._plain(event, "用法：/jmzip <漫画id|章节id> [-c 章节|章节:起页-止页]")
            return
        async for message in self._consume_download(
            event, target, "zip", chapter_expr
        ):
            yield message

    # ========== /jmcheckin 每日签到 ==========
    @staticmethod
    def _parse_record_day(value):
        """把签到记录里的日期（"01" / 1 / "2026-10-01"）统一成几号"""
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if 1 <= value <= 31 else None
        text = str(value).strip()
        if not text:
            return None
        if '-' in text or '/' in text:
            text = re.split(r'[-/]', text)[-1]
        digits = re.sub(r'\D', '', text)
        if not digits:
            return None
        day = int(digits)
        return day if 1 <= day <= 31 else None

    def _load_checkin_state(self):
        """读取本机签到状态，接口日历延迟时用于兜底"""
        try:
            if self.checkin_file.exists():
                data = json.loads(self.checkin_file.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
        except Exception:
            pass
        return {}

    def _save_checkin_state(self, streak, signed_count=None):
        """保存本机签到状态"""
        try:
            data = {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "streak": int(streak or 0),
            }
            if signed_count is not None:
                data["signed_count"] = signed_count
            self.checkin_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"保存签到状态失败: {e}")

    def _local_streak(self, today_signed):
        """接口未返回连续天数时，用本机记录估算"""
        state = self._load_checkin_state()
        streak = int(state.get("streak", 0) or 0)
        if today_signed:
            return max(1, streak)
        return max(0, streak)

    @staticmethod
    def _daily_payload(daily):
        """取出签到接口的字典数据，兼容 API（model_data）与 HTML（json()）"""
        try:
            if hasattr(daily, 'model_data'):
                data = daily.model_data
            elif hasattr(daily, 'json'):
                data = daily.json()
            else:
                data = None
        except Exception:
            data = None
        # jmcomic 的 AdvancedDict 不是 dict 子类，必须取 src_dict 后才能当字典用
        if hasattr(data, 'src_dict'):
            data = data.src_dict
        return data if isinstance(data, dict) else {}

    def _parse_daily_info(self, daily, assume_today_signed=False):
        """解析签到信息。
        HTML 客户端有 oldStep / error 字段，API 客户端没有，
        连续天数只能从 record 签到日历里按日期往前逐日统计。
        """
        data = self._daily_payload(daily)
        info = {
            'streak': None,
            'today_signed': False,
            'signed_count': None,
            'signed_days': set(),
            'progress': None,
        }
        if not data:
            return info

        progress = data.get('currentProgress') or data.get('current_progress')
        if progress is not None:
            info['progress'] = str(progress)

        # HTML 客户端直接给连续天数
        for key in ('oldStep', 'old_step', 'continuous', 'continuous_days', 'streak'):
            value = data.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                info['streak'] = int(value)
                break
            if isinstance(value, str) and value.strip().isdigit():
                info['streak'] = int(value.strip())
                break

        # 今日是否已签
        if data.get('error') == 'finished' or data.get('today_signed') is True:
            info['today_signed'] = True

        # 签到日历：API 是 record 二维数组，HTML 是 dateArray
        record = data.get('record')
        if hasattr(record, 'src_dict'):
            record = record.src_dict
        if isinstance(record, dict):
            record = list(record.values())
        if isinstance(record, list):
            for week in record:
                if hasattr(week, 'src_dict'):
                    week = week.src_dict
                days = week if isinstance(week, list) else [week]
                for day in days:
                    if hasattr(day, 'src_dict'):
                        day = day.src_dict
                    if not isinstance(day, dict) or not day.get('signed'):
                        continue
                    day_num = self._parse_record_day(day.get('date'))
                    if day_num is not None:
                        info['signed_days'].add(day_num)
        elif isinstance(data.get('dateArray'), list):
            for value in data['dateArray']:
                day_num = self._parse_record_day(value)
                if day_num is not None:
                    info['signed_days'].add(day_num)

        if info['signed_days']:
            info['signed_count'] = len(info['signed_days'])
            if datetime.now().day in info['signed_days']:
                info['today_signed'] = True

        if assume_today_signed:
            info['today_signed'] = True
            info['signed_days'].add(datetime.now().day)

        # 连续天数兜底：从今天（今天没签则从昨天）往前逐日数
        if info['streak'] is None and info['signed_days']:
            today = datetime.now().day
            cursor = today if today in info['signed_days'] else today - 1
            streak = 0
            while cursor >= 1 and cursor in info['signed_days']:
                streak += 1
                cursor -= 1
            info['streak'] = streak

        return info

    @filter.command("jmcheckin")
    async def jm_checkin(self, event: AstrMessageEvent):
        """每日签到领金币经验，用法：/jmcheckin"""
        if not self._check_permission(event):
            yield self._plain(event, self._permission_denied_text(event))
            return
        if not self._is_owner(event):
            yield self._plain(event, self._owner_required_text())
            return
        try:
            client = self._new_logged_client()

            # jmcomic 2.7.5 及以下版本不支持签到接口
            jm_version = getattr(jmcomic, '__version__', '未知')
            if not hasattr(client, 'get_daily') or not hasattr(client, 'daily_checkin'):
                yield self._plain(event,
                    f"⚠️ 当前 jmcomic 版本（{jm_version}）不支持每日签到功能\n"
                    f"需要 jmcomic >= 2.7.7（推荐升级到 2.7.9），请升级后重启 AstrBot：\n"
                    f"  D:\\Program Files\\AstrBot\\backend\\python\\python.exe -m pip install -U jmcomic"
                )
                return

            # 先获取签到信息
            yield self._plain(event, "📅 正在获取签到信息...")
            daily = await asyncio.to_thread(client.get_daily)
            info = self._parse_daily_info(daily)

            # 执行签到
            yield self._plain(event, "✅ 正在签到...")
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
                    final_info = self._parse_daily_info(daily_after, assume_today_signed=True)
                except Exception:
                    final_info = self._parse_daily_info(daily, assume_today_signed=True)
            else:
                final_info = info

            if final_info.get('signed_count') is None:
                final_info['signed_count'] = info.get('signed_count')
            if final_info.get('streak') is None:
                final_info['streak'] = info.get('streak')
            if final_info.get('streak') is None:
                final_info['streak'] = self._local_streak(bool(final_info.get('today_signed')))

            if signed_ok:
                streak_to_save = final_info.get('streak')
                if streak_to_save is None:
                    streak_to_save = 1
                self._save_checkin_state(streak_to_save, final_info.get('signed_count'))

            streak_text = f"{final_info['streak']} 天" if final_info.get('streak') is not None else "获取失败"
            reply = f"📅 每日签到\n\n{result_msg}\n\n"
            reply += f"🔥 连续签到：{streak_text}\n"
            if final_info.get('signed_count') is not None:
                reply += f"📆 本月已签：{final_info['signed_count']} 天"
            if final_info.get('today_signed'):
                reply += "\n\n✅ 今日已完成签到"

            yield self._plain(event, reply)

        except Exception as e:
            yield self._plain(event, self._handle_jm_exception(e, "签到"))

    # ========== /jmme 用户面板 ==========
    @filter.command("jmme")
    async def jm_user_info(self, event: AstrMessageEvent):
        """查看用户信息面板，用法：/jmme"""
        if not self._check_permission(event):
            yield self._plain(event, self._permission_denied_text(event))
            return
        if not self._is_owner(event):
            yield self._plain(event, self._owner_required_text())
            return
        try:
            client = self._new_logged_client()

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
                yield self._plain(event, "⚠️ 未配置登录账号，请在插件配置中填写账号密码")
                return

            # 获取签到信息（jmcomic 2.7.5+ 才支持）
            info = {}
            if hasattr(client, 'get_daily'):
                try:
                    daily = await asyncio.to_thread(client.get_daily)
                    info = self._parse_daily_info(daily)
                except Exception:
                    info = {}

                # 接口日历更新延迟时，用本机签到记录兜底
                state = self._load_checkin_state()
                today_str = datetime.now().strftime('%Y-%m-%d')
                if state.get('date') == today_str:
                    info['today_signed'] = True
                if info.get('streak') is None:
                    info['streak'] = self._local_streak(bool(info.get('today_signed')))
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

            yield self._plain(event, reply)

        except Exception as e:
            yield self._plain(event, self._handle_jm_exception(e, "获取用户信息"))

    # ========== /jmrank 排行榜 ==========
    @filter.command("jmrank")
    async def jm_ranking(self, event: AstrMessageEvent):
        """排行榜，用法：/jmrank day|week|month [页码]"""
        if not self._check_permission(event):
            yield self._plain(event, self._permission_denied_text(event))
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield self._plain(event,
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
            yield self._plain(event, "类型只能是 day / week / month")
            return
        if len(args) > 3:
            yield self._plain(event, "用法：/jmrank day|week|month [页码]\n页码只能填一个")
            return
        try:
            qq_page = self._parse_positive_page(args[2]) if len(args) >= 3 else 1
        except ValueError as e:
            yield self._plain(event, f"⚠️ {e}\n用法：/jmrank {rank_type} [页码]")
            return

        rank_name, method_name = rank_map[rank_type]
        yield self._plain(event, f"🏆 获取{rank_name} 第{qq_page}页...")

        try:
            client = self._new_logged_client()
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
                yield self._plain(event, "没有更多了")
                return

            results = []
            for i, (aid, title, tags) in enumerate(page_items):
                global_index = (qq_page - 1) * PER_QQ_PAGE + i + 1
                title = self._safe_text(title, self.convert_title)
                tags = [self._safe_text(t, self.convert_tags) for t in tags]
                safe_tags = tags[:3]
                tag_str = f" [{', '.join(safe_tags)}]" if safe_tags else ""
                results.append(f"{global_index}. [JM{aid}] {title}{tag_str}")

            reply = f"🏆 {rank_name}（第{qq_page}页）\n\n" + "\n".join(results)
            reply += f"\n\n下一页：/jmrank {rank_type} {qq_page + 1}"
            yield self._plain(event, reply)

        except Exception as e:
            yield self._plain(event, self._handle_jm_exception(e, "获取排行榜"))

    # ========== /jmhis 下载历史 ==========
    @filter.command("jmhis")
    async def jm_history(self, event: AstrMessageEvent):
        """查看下载历史，用法：/jmhis 或 /jmhis clear 清空历史"""
        if not self._check_permission(event):
            yield self._plain(event, self._permission_denied_text(event))
            return
        args = event.message_str.split()
        user_id = event.get_sender_id()

        if len(args) >= 2 and args[1].lower() == "clear":
            data = self._load_history()
            if user_id in data:
                del data[user_id]
            self._save_history(data)
            yield self._plain(event, "🧹 你的下载历史已清空")
            return

        # /jmhis -all：管理员查看所有成员的历史
        if len(args) >= 2 and args[1].lower() in ("-all", "all"):
            is_admin = event.is_admin() if hasattr(event, 'is_admin') else getattr(event, 'role', '') == 'admin'
            if not is_admin:
                yield self._plain(event, "⚠️ 只有管理员可以使用 /jmhis -all")
                return
            data = self._load_history()
            if not data:
                yield self._plain(event, "还没有任何下载记录")
                return
            lines = []
            total_records = 0
            total_users = 0
            for uid, items in data.items():
                if not items:
                    continue
                total_users += 1
                total_records += len(items)
                lines.append(f"👤 用户 {uid}（{len(items)}条）")
                for index, item in enumerate(items, 1):
                    title = self._safe_text(item.get('title', ''), self.convert_title)
                    lines.append(
                        f"  {index}. [JM{item.get('id', '')}] {title[:30]}  "
                        f"({item.get('size_mb', '?')}MB)  {item.get('time', '')}"
                    )
                lines.append("")
            reply = (
                f"📜 全部成员下载历史（{total_users}人，共{total_records}条）\n\n"
                + "\n".join(lines)
            ).rstrip()
            if len(reply) > 3500:
                reply = reply[:3450] + "\n...（消息过长，已截断）"
            yield self._plain(event, reply)
            return

        if len(args) > 2:
            yield self._plain(event, "用法：/jmhis [页码]\n页码只能填一个")
            return
        try:
            page_num = self._parse_positive_page(args[1]) if len(args) >= 2 else 1
        except ValueError as e:
            yield self._plain(event, f"⚠️ {e}\n用法：/jmhis [页码]")
            return

        history = self._get_user_history(user_id)
        if not history:
            yield self._plain(event, "还没有下载记录")
            return

        page_size = 20
        total_pages = max(1, math.ceil(len(history) / page_size))
        if page_num > total_pages:
            yield self._plain(event,
                f"⚠️ 下载历史共 {len(history)} 条，只有 {total_pages} 页，已到最后一页\n"
                f"🔙 回到最后一页：/jmhis {total_pages}"
            )
            return

        start = (page_num - 1) * page_size
        page_items = history[start:start + page_size]
        results = []
        for offset, item in enumerate(page_items, 1):
            title = self._safe_text(item.get('title', ''), self.convert_title)
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
        yield self._plain(event, reply)

    # ========== /jmc 评论查看 ==========
    @filter.command("jmc")
    async def jm_comments(self, event: AstrMessageEvent):
        """查看评论，用法：/jmc <id> [页码]
        支持翻页，展示回复楼、剧透标记、点赞数
        """
        if not self._check_permission(event):
            yield self._plain(event, self._permission_denied_text(event))
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield self._plain(event, "用法：/jmc <漫画id> [页码]\n示例：/jmc 123456 或 /jmc 123456 2")
            return

        prefix, album_id = self._normalize_target(args[1])
        if prefix == "p" or not album_id.isdigit():
            yield self._plain(event,
                f"❌ 漫画ID必须是数字：{args[1]}\n"
                "章节ID不能查评论，请用 /jm <章节id> 下载"
            )
            return
        if len(args) > 3:
            yield self._plain(event, "用法：/jmc <漫画id> [页码]\n页码只能填一个")
            return
        try:
            page_num = self._parse_positive_page(args[2]) if len(args) >= 3 else 1
        except ValueError as e:
            yield self._plain(event, f"⚠️ {e}\n用法：/jmc {album_id} [页码]")
            return

        # HTML 客户端不支持评论翻页，最多10条
        if page_num > 1 and self.client_type != 'api':
            yield self._plain(event, "⚠️ 评论翻页仅支持 API 客户端，请在插件配置中将客户端类型改为 api")
            return

        yield self._plain(event, f"💬 获取 JM{album_id} 的评论（第{page_num}页）...")

        try:
            client = self._new_logged_client()

            # 先校验漫画是否存在，避免无效ID被上游当成“0页评论”
            album = await asyncio.to_thread(client.get_album_detail, album_id)
            if self._looks_like_chapter_album(album):
                parent_id = await self._resolve_chapter_album_parent(client, album, album_id)
                parent_line = (
                    f"该章节所属漫画：JM{parent_id}，可用 /jmv {parent_id} 查看。\n"
                    if parent_id else ""
                )
                yield self._plain(event,
                    f"❌ JM{album_id} 看起来是章节ID，不是漫画ID。\n"
                    f"{parent_line}"
                    f"章节ID不能查评论，请用 /jm {album_id} 下载。"
                )
                return

            # 超页检测：如果已知总页数且超过，直接提示（避免API返回重复内容）
            comment_cache_key = f"comment_pages_{album_id}"
            known_max_page = getattr(self, '_comment_page_cache', {}).get(comment_cache_key)
            if known_max_page and page_num > known_max_page:
                yield self._plain(event, f"⚠️ JM{album_id} 评论共 {known_max_page} 页，已到最后一页")
                return

            page = await asyncio.to_thread(client.album_pagination, album_id, page=page_num)

            # 先拿到总页数并缓存（API 的 page_count 由 total 计算，最多500页）
            total = getattr(page, 'total', None)
            page_count = getattr(page, 'page_count', None)
            if page_count:
                if not hasattr(self, '_comment_page_cache'):
                    self._comment_page_cache = {}
                self._comment_page_cache[comment_cache_key] = page_count

            # 0 条评论不是“超页”，优先显示自然文案
            if page_count == 0:
                yield self._plain(event, "还没有评论")
                return

            # 请求页码超过总页数时，上游会重复返回第一页内容，这里直接拦截
            if page_count is not None and page_num > page_count:
                yield self._plain(event,
                    f"⚠️ JM{album_id} 评论共 {page_count} 页，已到最后一页\n"
                    f"🔙 回到第1页：/jmc {album_id} 1"
                )
                return

            comments = list(page)
            if not comments:
                if page_num > 1:
                    yield self._plain(event,
                        f"⚠️ 第{page_num}页没有评论，已经到最后一页了\n"
                        f"🔙 上一页：/jmc {album_id} {page_num - 1}"
                    )
                else:
                    yield self._plain(event, "还没有评论")
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
                nickname = self._safe_text(nickname, False)
                raw_content = getattr(comment, 'content', '') or ''
                content = self._safe_text(raw_content.replace('\n', ' ').strip()[:120], self.convert_comment)

                reply_to = ""
                parent_id = getattr(comment, 'parent_comment_id', None)
                if is_reply:
                    parent_name = id_to_name.get(str(parent_id)) if parent_id is not None else None
                    if parent_name:
                        parent_name = self._safe_text(parent_name, False)
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

            yield self._plain(event, reply[:3500])

        except Exception as e:
            yield self._plain(event, self._handle_jm_exception(e, "获取评论"))

    # ========== /jmwhoami 查看自己的ID ==========
    @filter.command("jmwhoami")
    async def jm_whoami(self, event: AstrMessageEvent):
        """查看自己的发送者ID，用于配置 owner_ids"""
        if not self._check_permission(event):
            yield self._plain(event, self._permission_denied_text(event))
            return
        sender_id = str(event.get_sender_id())
        is_admin = event.is_admin() if hasattr(event, 'is_admin') else getattr(event, 'role', '') == 'admin'
        is_owner = self._is_owner(event)
        lines = [
            f"🆔 你的发送者ID：{sender_id}",
            f"👑 AstrBot管理员：{'是' if is_admin else '否'}",
            f"🔑 JM账号号主：{'是' if is_owner else '否'}",
        ]
        if not self.owner_ids:
            lines.append("\n⚠️ 尚未配置 owner_ids，账号类命令暂时不可用。")
            lines.append(f"把上面的ID填入插件配置 owner_ids 即可。")
        yield self._plain(event, "\n".join(lines))

    # ========== /jmdomain 查看可用域名 ==========
    @filter.command("jmdomain")
    async def jm_domain(self, event: AstrMessageEvent):
        """查看 API 与 HTML 两套可用域名"""
        if not self._check_permission(event):
            yield self._plain(event, self._permission_denied_text(event))
            return
        yield self._plain(event, "🌐 正在获取可用域名...")
        try:
            client = self._new_logged_client()
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

            yield self._plain(event, reply)
        except Exception as e:
            yield self._plain(event, self._handle_jm_exception(e, "获取域名"))

    # ========== /jmupdate 查看更新日志 ==========
    @filter.command("jmupdate")
    async def jm_changelog(self, event: AstrMessageEvent):
        """查看插件更新日志"""
        if not self._check_permission(event):
            yield self._plain(event, self._permission_denied_text(event))
            return
        changelog_path = Path(__file__).parent / "CHANGELOG.md"
        if not changelog_path.exists():
            yield self._plain(event, "❌ 更新日志文件不存在")
            return
        content = changelog_path.read_text(encoding="utf-8")
        # 只发最新版本：从第一个二级标题开始，到下一个二级标题前结束
        match = re.search(r"^##\s+.*?(?=^##\s+|\Z)", content, re.S | re.M)
        reply = match.group(0).strip() if match else content.strip()
        reply = re.sub(r"\n+---\s*$", "", reply).strip()
        if len(reply) > 1800:
            reply = reply[:1750] + "\n...（已截断）"
        yield self._plain(event, f"📝 最新更新日志：\n\n{reply}")

    # ========== /jmfav 收藏夹 ==========
    def _clear_client_cache(self, client):
        """收藏状态是 toggle 接口，操作前后必须清缓存，避免 is_favorite 读到旧值"""
        try:
            cache = client.get_cache_dict()
            if cache:
                cache.clear()
        except Exception:
            pass

    async def _favorite_state(self, client, album_id):
        """返回 (是否已收藏, 漫画详情)"""
        self._clear_client_cache(client)
        album = await asyncio.to_thread(client.get_album_detail, album_id)
        return bool(getattr(album, 'is_favorite', False)), album

    async def _render_favorites(self, client, folder_id='0', title='⭐ 当前收藏', limit=10):
        """渲染收藏夹内容，操作后自动调用"""
        page = await asyncio.to_thread(client.favorite_folder, 1, folder_id=folder_id)
        total = getattr(page, 'total', None)
        items = list(page.iter_id_title_tag())
        if not items:
            return f"{title}：空"

        results = []
        for i, (aid, item_title, tags) in enumerate(items[:limit]):
            item_title = self._safe_text(item_title, self.convert_title)
            tags = [self._safe_text(t, self.convert_tags) for t in tags]
            safe_tags = tags[:3]
            tag_str = f" [{', '.join(safe_tags)}]" if safe_tags else ""
            results.append(f"{i+1}. [JM{aid}] {item_title}{tag_str}")

        total_text = total if total is not None else len(items)
        reply = f"{title}（共{total_text}本，显示前{len(results)}本）：\n\n" + "\n".join(results)
        reply += "\n\n/jmv <id> 查详情，/jm <id> 下载"
        return reply

    def _configured_username(self):
        try:
            for plugin in self.option.plugins.after_init:
                if plugin.get('plugin') == 'login':
                    return plugin.get('kwargs', {}).get('username', '') or ''
        except Exception:
            pass
        return ''

    @staticmethod
    def _collect_favorite_rows(client, folder_id):
        """同步收集收藏夹全部条目，供 asyncio.to_thread 调用。"""
        rows = []
        seen = set()
        for page in client.favorite_folder_gen(folder_id=folder_id):
            for aid, extra in page.content:
                aid = str(aid)
                if aid in seen:
                    continue
                seen.add(aid)
                rows.append((
                    aid,
                    str(extra.get('author', '') or ''),
                    str(extra.get('name', '') or ''),
                ))
        return rows

    @staticmethod
    def _write_favorites_csv(path, rows):
        """写 CSV；utf-8-sig 方便 Windows Excel 直接打开。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['id', 'author', 'name'])
            writer.writerows(rows)

    async def _export_favorites(self, event: AstrMessageEvent, folder_id: str):
        """导出收藏夹为 CSV 文件（仅号主，无加密）。"""
        try:
            client = self._new_logged_client()
            if not self._configured_username():
                yield self._plain(event, "⚠️ 未配置登录账号，无法使用收藏夹功能")
                return

            folder_name = "全部"
            if folder_id != "0":
                folder_name = f"收藏夹{folder_id}"
                try:
                    page = await asyncio.to_thread(client.favorite_folder, 1)
                    for fid, fname in page.iter_folder_id_name():
                        if str(fid) == str(folder_id):
                            folder_name = fname or folder_name
                            break
                except Exception as e:
                    logger.warning(f"读取收藏夹名称失败: {e}")

            yield self._plain(event, f"📤 正在读取收藏夹「{folder_name}」...")
            rows = await asyncio.to_thread(
                self._collect_favorite_rows, client, folder_id
            )
            if not rows:
                yield self._plain(event, f"ℹ️ 收藏夹「{folder_name}」为空，没有可导出的内容")
                return

            export_dir = self.data_dir / "exports"
            safe_name = self._safe_filename(folder_name or f"收藏夹{folder_id}")
            output_path = export_dir / f"JM收藏夹_{folder_id}_{safe_name}.csv"
            await asyncio.to_thread(self._write_favorites_csv, output_path, rows)

            size_kb = max(1, math.ceil(output_path.stat().st_size / 1024))
            self._record_file_send()
            yield self._chain(event, [
                Comp.Plain(f"📎 {output_path.name} ({size_kb}KB，共{len(rows)}本)\n"),
                Comp.File(file=str(output_path), name=output_path.name),
            ])
            output_path.unlink(missing_ok=True)
            await asyncio.sleep(self.send_interval)
            yield self._plain(event, f"✅ 已导出收藏夹「{folder_name}」，共 {len(rows)} 本")
        except Exception as e:
            yield self._plain(event, self._handle_jm_exception(e, "导出收藏夹"))

    # ========== /jmsub 订阅更新 ==========
    def _load_subscriptions(self):
        try:
            data = json.loads(self.subscription_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"读取订阅文件失败: {e}")
            return {}

    def _save_subscriptions(self, data):
        try:
            tmp = self.subscription_file.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.subscription_file)
        except Exception as e:
            logger.warning(f"保存订阅文件失败: {e}")

    @staticmethod
    def _album_photo_ids(album):
        result = []
        try:
            for photo in album:
                photo_id = str(getattr(photo, "photo_id", "") or "")
                if photo_id:
                    result.append(photo_id)
        except Exception:
            pass
        return result

    def _subscription_snapshot(self, album):
        try:
            title = str(getattr(album, "name", "") or "")
        except Exception:
            title = ""
        return {
            "title": title,
            "photo_ids": self._album_photo_ids(album),
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    async def _check_subscriptions_once(self):
        """检查所有会话的订阅，有新增章节时推送到对应会话。"""
        data = self._load_subscriptions()
        if not data:
            return
        if self._subscription_lock is None:
            self._subscription_lock = asyncio.Lock()
        async with self._subscription_lock:
            client = await asyncio.to_thread(self._new_logged_client)
            changed = False
            for umo, subs in list(data.items()):
                if not isinstance(subs, dict):
                    continue
                for album_id, info in list(subs.items()):
                    if not isinstance(info, dict):
                        continue
                    try:
                        album = await asyncio.to_thread(
                            client.get_album_detail, album_id
                        )
                        old_ids = {str(x) for x in (info.get("photo_ids") or [])}
                        photos = list(album)
                        new_photos = [
                            p for p in photos
                            if str(getattr(p, "photo_id", "")) not in old_ids
                        ]
                        if not new_photos:
                            continue

                        title = self._safe_text(
                            getattr(album, "name", "") or info.get("title", ""),
                            self.convert_title,
                        ) or f"JM{album_id}"
                        lines = [
                            f"🔔 订阅更新：《{title}》新增 {len(new_photos)} 章"
                        ]
                        for i, photo in enumerate(new_photos[:10], 1):
                            chapter_name = self._safe_text(
                                getattr(photo, "name", "") or "",
                                self.convert_title,
                            )
                            index = getattr(photo, "index", None) or i
                            lines.append(f"  第{index}章 {chapter_name}".rstrip())
                        if len(new_photos) > 10:
                            lines.append(f"  ... 共 {len(new_photos)} 章")
                        lines.append("")
                        lines.append(f"/jmv {album_id} 查看详情")
                        text = self._safe_text("\n".join(lines))

                        sent = await self.context.send_message(
                            umo, MessageChain(chain=[Comp.Plain(text)])
                        )
                        if sent is False:
                            logger.warning(f"订阅推送未找到可用平台: {umo}")
                            continue

                        info["photo_ids"] = self._album_photo_ids(album)
                        info["title"] = str(getattr(album, "name", "") or "")
                        info["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
                        changed = True
                    except Exception as e:
                        logger.warning(f"检查订阅 JM{album_id} 失败: {e}")
                    # 每本之间稍作间隔，避免集中请求触发限流
                    await asyncio.sleep(2)
            if changed:
                self._save_subscriptions(data)

    async def _subscription_loop(self):
        while True:
            interval = max(0, int(self.subscribe_interval or 0))
            if interval <= 0:
                return
            try:
                await asyncio.sleep(interval)
                await self._check_subscriptions_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"订阅检查失败: {e}")
                await asyncio.sleep(60)

    @filter.command("jmsub")
    async def jm_subscribe(self, event: AstrMessageEvent):
        """订阅漫画更新，用法：
        /jmsub <漫画id> → 订阅/更新基线
        /jmsub list → 查看当前会话订阅
        /jmsub del <漫画id> → 取消订阅
        """
        if not self._check_permission(event):
            yield self._plain(event, self._permission_denied_text(event))
            return
        args = event.message_str.split()
        umo = event.unified_msg_origin
        data = self._load_subscriptions()
        subs = data.get(umo)
        if not isinstance(subs, dict):
            subs = {}

        action = args[1].lower() if len(args) >= 2 else "list"
        if action in ("list", "ls"):
            if not subs:
                yield self._plain(event,
                    "当前会话还没有订阅。\n"
                    "用法：/jmsub <漫画id> 订阅更新"
                )
                return
            lines = [f"🔔 当前会话订阅（{len(subs)}）："]
            for album_id, info in subs.items():
                title = self._safe_text(
                    (info or {}).get("title", "") or "未知", self.convert_title
                )
                count = len((info or {}).get("photo_ids") or [])
                lines.append(f"  JM{album_id} {title}（{count}章）")
            lines.append("")
            lines.append("💡 /jmsub <漫画id> 更新基线，/jmsub del <漫画id> 取消")
            yield self._plain(event, "\n".join(lines))
            return

        if action in ("del", "remove", "unsub"):
            if len(args) != 3:
                yield self._plain(event, "用法：/jmsub del <漫画id>")
                return
            _, album_id = self._normalize_target(args[2])
            if not album_id.isdigit() or int(album_id) <= 0:
                yield self._plain(event, f"❌ 漫画ID必须是大于 0 的数字：{args[2]}")
                return
            if album_id not in subs:
                yield self._plain(event, f"ℹ️ 当前会话没有订阅 JM{album_id}")
                return
            subs.pop(album_id, None)
            if subs:
                data[umo] = subs
            else:
                data.pop(umo, None)
            self._save_subscriptions(data)
            yield self._plain(event, f"✅ 已取消订阅 JM{album_id}")
            return

        if len(args) != 2:
            yield self._plain(event,
                "用法：\n"
                "  /jmsub <漫画id>        订阅更新\n"
                "  /jmsub list            查看当前会话订阅\n"
                "  /jmsub del <漫画id>    取消订阅"
            )
            return

        prefix, album_id = self._normalize_target(args[1])
        if prefix == "p" or not album_id.isdigit() or int(album_id) <= 0:
            yield self._plain(event,
                f"❌ 漫画ID必须是大于 0 的数字：{args[1]}\n"
                "章节ID不能订阅，请填所属漫画ID"
            )
            return

        yield self._plain(event, f"🔔 正在读取 JM{album_id} 的章节信息...")
        try:
            client = await asyncio.to_thread(self._new_logged_client)
            album = await asyncio.to_thread(client.get_album_detail, album_id)
            if self._looks_like_chapter_album(album):
                parent_id = await self._resolve_chapter_album_parent(
                    client, album, album_id
                )
                if parent_id:
                    yield self._plain(event,
                        f"❌ JM{album_id} 是章节ID，不能订阅。\n"
                        f"所属漫画：JM{parent_id}\n"
                        f"请用 /jmsub {parent_id} 订阅整本更新。"
                    )
                else:
                    yield self._plain(event,
                        f"❌ JM{album_id} 是章节ID，不能订阅。请改用所属漫画ID。"
                    )
                return

            snapshot = self._subscription_snapshot(album)
            if not snapshot["photo_ids"]:
                yield self._plain(event,
                    f"⚠️ JM{album_id} 没有章节列表，暂时无法订阅"
                )
                return

            existed = album_id in subs
            subs[album_id] = snapshot
            data[umo] = subs
            self._save_subscriptions(data)

            title = self._safe_text(
                snapshot["title"] or f"JM{album_id}", self.convert_title
            )
            if self.subscribe_interval > 0:
                interval_text = (
                    f"每 {self.subscribe_interval} 秒检查一次"
                    if self.subscribe_interval < 3600
                    else f"每 {self.subscribe_interval // 3600} 小时检查一次"
                )
                hint = f"后台会{interval_text}，发现新章节后推送到当前会话。"
            else:
                hint = "后台自动检查已关闭（subscribe_interval=0），当前只保存订阅，不会推送。"
            yield self._plain(event,
                f"{'🔄 已更新订阅基线' if existed else '✅ 已订阅'}："
                f"《{title}》JM{album_id}\n"
                f"当前 {len(snapshot['photo_ids'])} 章。{hint}"
            )
        except Exception as e:
            yield self._plain(event, self._handle_jm_exception(e, "订阅"))

    @filter.command("jmfav")
    async def jm_favorite(self, event: AstrMessageEvent):
        """收藏夹功能：
        /jmfav → 查看收藏夹文件夹列表
        /jmfav <文件夹id> → 查看文件夹里的内容
        /jmfav add <漫画id> → 把漫画加入收藏夹
        /jmfav del <漫画id> → 从收藏夹移除
        /jmfav export [文件夹id] → 导出收藏夹 CSV（30 秒内二次确认）
        """
        if not self._check_permission(event):
            yield self._plain(event, self._permission_denied_text(event))
            return
        if not self._is_owner(event):
            yield self._plain(event, self._owner_required_text())
            return
        args = event.message_str.split()

        operation = args[1].lower() if len(args) >= 2 else ""
        if operation in ("add", "del"):
            if len(args) != 3:
                yield self._plain(event,
                    f"用法：/jmfav {operation} <漫画id>\n"
                    "示例：/jmfav add 123456"
                )
                return
            _, album_id = self._normalize_target(args[2])
            if not album_id.isdigit() or int(album_id) <= 0:
                yield self._plain(event, f"❌ 漫画ID必须是大于 0 的数字：{args[2]}")
                return
        elif operation == "export":
            if len(args) > 3:
                yield self._plain(event,
                    "用法：/jmfav export [文件夹id]\n"
                    "不带文件夹id时导出“全部”收藏"
                )
                return
            folder_id = "0"
            if len(args) == 3:
                token = args[2]
                if not re.fullmatch(r"\d+", token):
                    yield self._plain(event, f"❌ 文件夹ID必须是数字：{token}")
                    return
                folder_id = str(int(token))

            # 二次确认：首次输入只提示，30 秒内再次输入相同命令才真正导出
            user_key = (str(event.get_sender_id()), event.unified_msg_origin)
            action_key = f"export:{folder_id}"
            key = (*user_key, action_key)
            now = time.monotonic()
            self._pending_exports = {
                k: v for k, v in self._pending_exports.items()
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
            if key not in self._pending_exports:
                self._pending_exports[key] = now
                yield self._plain(event,
                    f"⚠️ 即将导出收藏夹 [ID:{folder_id}] 为 CSV 文件。\n"
                    "导出会把账号收藏数据写入文件并发送到当前会话，请确认不是误触。\n"
                    f"请在 30 秒内再次输入完全相同的命令确认：\n{command_text}"
                )
                return
            self._pending_exports.pop(key, None)
            async for item in self._export_favorites(event, folder_id):
                yield item
            return
        elif len(args) >= 2:
            if len(args) > 2:
                yield self._plain(event, "用法：/jmfav [文件夹id]\n文件夹ID只能填一个")
                return
            folder_token = args[1]
            if not re.fullmatch(r"\d+", folder_token):
                yield self._plain(event,
                    f"❌ 无法识别的收藏夹参数：{folder_token}\n"
                    "用法：/jmfav | /jmfav <文件夹id> | /jmfav add <漫画id> | "
                    "/jmfav del <漫画id> | /jmfav export [文件夹id]"
                )
                return
            if int(folder_token) < 0:
                yield self._plain(event, "❌ 文件夹ID不能是负数")
                return

        try:
            client = self._new_logged_client()

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
                yield self._plain(event, "⚠️ 未配置登录账号，无法使用收藏夹功能")
                return

            # /jmfav add <id> — 添加收藏（仅API客户端支持）
            if len(args) >= 3 and args[1].lower() == 'add':
                if self.client_type != 'api':
                    yield self._plain(event, "⚠️ 添加收藏仅支持 API 客户端，请在插件配置中将客户端类型改为 api")
                    return
                _, album_id = self._normalize_target(args[2])
                yield self._plain(event, f"⭐ 正在检查 JM{album_id} 的收藏状态...")
                is_fav, _album = await self._favorite_state(client, album_id)
                if is_fav:
                    yield self._plain(event, f"ℹ️ JM{album_id} 已经在收藏夹里了")
                else:
                    await asyncio.to_thread(client.add_favorite_album, album_id, folder_id='0')
                    self._clear_client_cache(client)
                    yield self._plain(event, f"✅ 已收藏 JM{album_id}")
                try:
                    yield self._plain(event, await self._render_favorites(client, '0'))
                except Exception as list_err:
                    logger.warning(f"刷新收藏夹失败: {list_err}")
                return

            # /jmfav del <id> — 删除收藏（仅API客户端支持，且需jmcomic较新版本）
            if len(args) >= 3 and args[1].lower() == 'del':
                if self.client_type != 'api':
                    yield self._plain(event, "⚠️ 移除收藏仅支持 API 客户端，请在插件配置中将客户端类型改为 api")
                    return
                if not hasattr(client, 'delete_favorite_album'):
                    yield self._plain(event, "⚠️ 当前 jmcomic 版本不支持移除收藏，请升级 jmcomic 到最新版本")
                    return
                _, album_id = self._normalize_target(args[2])
                yield self._plain(event, f"🗑️ 正在检查 JM{album_id} 的收藏状态...")
                is_fav, _album = await self._favorite_state(client, album_id)
                if not is_fav:
                    yield self._plain(event, f"ℹ️ JM{album_id} 本来就不在收藏夹里，无需移除")
                else:
                    await asyncio.to_thread(client.delete_favorite_album, album_id, folder_id='0')
                    self._clear_client_cache(client)
                    yield self._plain(event, f"✅ 已移除收藏 JM{album_id}")
                try:
                    yield self._plain(event, await self._render_favorites(client, '0'))
                except Exception as list_err:
                    logger.warning(f"刷新收藏夹失败: {list_err}")
                return

            # /jmfav <folder_id> — 查看指定文件夹内容
            if len(args) >= 2 and args[1].isdigit():
                folder_id = args[1]
                yield self._plain(event, f"⭐ 获取收藏夹 [ID:{folder_id}] 的内容...")
                yield self._plain(event,
                    await self._render_favorites(
                        client, folder_id, f"⭐ 收藏夹 [ID:{folder_id}]", 20
                    )
                )
                return

            # /jmfav — 列出收藏夹文件夹
            yield self._plain(event, "⭐ 获取我的收藏夹列表...")
            page = await asyncio.to_thread(client.favorite_folder, 1)

            # 列出所有收藏夹文件夹
            folders = list(page.iter_folder_id_name())
            if not folders:
                # 如果没有文件夹列表，直接显示全部收藏
                yield self._plain(event,
                    await self._render_favorites(client, '0', "⭐ 我的收藏", 20)
                )
                return

            # 显示文件夹列表
            folder_lines = []
            for fid, fname in folders:
                fname_cn = self._safe_text(fname, self.convert_title)
                folder_lines.append(f"  📁 [ID:{fid}] {fname_cn}")

            reply = "⭐ 我的收藏夹文件夹：\n\n" + "\n".join(folder_lines)
            reply += "\n\n💡 /jmfav <文件夹id> 查看内容，/jmfav add <漫画id> 添加收藏"
            yield self._plain(event, reply)

        except Exception as e:
            yield self._plain(event, self._handle_jm_exception(e, "收藏夹操作"))

    async def initialize(self):
        """插件激活时启动订阅更新检查。"""
        if self.subscribe_interval > 0 and self._subscribe_task is None:
            if self._subscription_lock is None:
                self._subscription_lock = asyncio.Lock()
            self._subscribe_task = asyncio.create_task(self._subscription_loop())
            logger.info(f"订阅更新检查已启动，间隔 {self.subscribe_interval} 秒")

    async def terminate(self):
        task = self._subscribe_task
        self._subscribe_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"停止订阅检查任务时出错: {e}")

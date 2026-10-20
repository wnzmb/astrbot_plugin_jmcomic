import os
import asyncio
import zipfile
import shutil
import math
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

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


class ProgressDownloader(JmAsyncDownloader):
    """带进度跟踪的下载器，进度存在类变量中按 album_id 隔离
    适配 jmcomic 2.7.9+ 的异步下载器架构（async 上下文管理器 + 异步钩子）
    """
    # key: album_id, value: {'total': int, 'done': int, 'finished': bool}
    _progress = {}
    # key: album_id, value: 本次下载实际写入的漫画根目录（不依赖 dir_rule 命名）
    _album_dirs = {}

    async def before_album(self, album):
        await super().before_album(album)
        total = 0
        for photo in album:
            if photo.page_arr:
                total += len(photo.page_arr)
        ProgressDownloader._progress[str(album.album_id)] = {
            'total': total,
            'done': 0,
            'finished': False,
        }

    async def after_image(self, image, img_save_path):
        await super().after_image(image, img_save_path)
        try:
            album_id = str(image.from_photo.album_id)
            if album_id in ProgressDownloader._progress:
                ProgressDownloader._progress[album_id]['done'] += 1

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
        if aid in ProgressDownloader._progress:
            ProgressDownloader._progress[aid]['finished'] = True


@register("astrbot_plugin_jmcomic", "wnzmb", "漫画下载插件", "0.6.4")
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
        photo_threads = int(self.config.get("photo_threads", 8))
        image_threads = int(self.config.get("image_threads", 20))
        self.per_page = int(self.config.get("per_page", 10))
        self.allow_group = self.config.get("allow_group", True)
        self.convert_enabled = self.config.get("convert_enabled", True)
        self.convert_title = self.config.get("convert_title", True)
        self.convert_tags = self.config.get("convert_tags", True)
        self.convert_author = self.config.get("convert_author", True)
        self.convert_comment = self.config.get("convert_comment", True)
        self.sensitive_filter = self.config.get("sensitive_filter", True)
        self.tease_enabled = self.config.get("tease_enabled", True)
        self.cache_days = int(self.config.get("cache_days", 7))
        self.comment_count = int(self.config.get("comment_count", 10))
        self.history_max = int(self.config.get("history_max", 50))
        self.dual_search = self.config.get("dual_search", True)

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

        option_dict = {
            "log": False,
            "client": {
                "impl": self.client_type,
                "retry_times": 5,
                "cache": True,
                "domain": domain_conf,
                "postman": {
                    "meta_data": {
                        "proxies": None,
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
                # 目录名带漫画ID：JM{id}_{标题}/章节名，便于按ID精确查找缓存
                # 默认规则 Bd_Pname 只有标题，会导致下载完成后无法按ID定位目录
                "rule": "Bd/JM{Aid}_{Atitle}/Pname",
                "normalize_zh": "zh-cn"
            },
            "plugins": {
                "after_init": []
            }
        }

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

        logger.info(f"JMComic 插件初始化完成 v0.6.4，客户端: {self.client_type.upper()}，镜像站: {custom_domains}")

        # 历史记录文件
        self.history_file = self.data_dir / "history.json"
        if not self.history_file.exists():
            self.history_file.write_text("{}", encoding="utf-8")

        # 最近一次成功签到的本地记录（API 签到日历更新有延迟时兜底）
        self.checkin_file = self.data_dir / "checkin_state.json"

    def _check_permission(self, event: AstrMessageEvent):
        """检查群聊权限"""
        if not self.allow_group:
            # 群聊会有group_id，私聊没有
            group_id = event.get_group_id()
            if group_id:
                return False
        return True

    def _handle_jm_exception(self, e: Exception, action: str = "操作") -> str:
        """把 jmcomic 异常转换为用户友好的提示"""
        if isinstance(e, MissingAlbumPhotoException):
            return f"❌ 查无此本/章节，请检查 ID 是否正确"
        elif isinstance(e, RequestRetryAllFailException):
            return f"❌ 网络请求全部失败，可尝试在插件配置中更换域名\n{e}"
        elif isinstance(e, PartialDownloadFailedException):
            return f"⚠️ 部分图片下载失败，已发送已完成部分"
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

        # 2) 按 JM{id}_ 前缀匹配（当前 dir_rule 生成的目录名）
        candidates = []
        for folder in self.download_base_dir.glob(f"JM{album_id}_*"):
            if folder.is_dir() and folder.resolve() != base_resolved:
                candidates.append(folder)
        if candidates:
            candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            for folder in candidates:
                if self._album_dir_has_images(folder):
                    return folder

        return None

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

    def _safe_filename(self, name, max_len=60):
        """生成安全的文件名：敏感词转拼音并去掉非法字符"""
        name = self._safe_text(name) or ''
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

    async def _download_batch(self, event, id_str):
        """批量下载：逗号分隔的ID列表，逐本下载逐本发送"""
        id_list = [x.strip() for x in id_str.split(',') if x.strip()]
        if not id_list:
            yield event.plain_result("❌ 未解析到有效ID")
            return

        yield event.plain_result(f"📦 批量下载 {len(id_list)} 本：{', '.join(id_list)}")

        success_count = 0
        failed_ids = []

        for idx, album_id in enumerate(id_list):
            try:
                yield event.plain_result(f"⬇️ [{idx+1}/{len(id_list)}] 开始下载 JM{album_id} ...")

                # 检查缓存或下载后按ID查找目录
                album_dir = self._find_album_dir(album_id)
                if album_dir is None:
                    result = await jmcomic.download_album_async(album_id, self.option, downloader=ProgressDownloader)
                    album_dir = self._find_album_dir(album_id)

                if album_dir is None:
                    failed_ids.append(album_id)
                    yield event.plain_result(f"❌ [{idx+1}/{len(id_list)}] JM{album_id} 下载后仍找不到目录")
                    continue

                # 目录名形如 JM{id}_{标题}，取出标题用于压缩包名/历史记录
                album_title = album_dir.name
                dir_prefix = f"JM{album_id}_"
                if album_title.startswith(dir_prefix):
                    album_title = album_title[len(dir_prefix):]
                if not album_title:
                    album_title = str(album_id)
                album_title = self._safe_text(album_title, self.convert_title)

                # 打包ZIP
                zip_name = f"JM{album_id}_{self._safe_filename(album_title, 30)}.zip"
                zip_path = self.zip_base_dir / zip_name
                await asyncio.to_thread(self._zip_folder, album_dir, zip_path)
                size_mb = zip_path.stat().st_size / 1024 / 1024

                yield event.plain_result(f"✅ [{idx+1}/{len(id_list)}] JM{album_id} 完成，{size_mb:.1f}MB，发送中...")
                yield event.chain_result([
                    Comp.Plain(f"📎 {zip_name}\n"),
                    Comp.File(file=str(zip_path), name=zip_name)
                ])
                zip_path.unlink(missing_ok=True)
                success_count += 1

                # 写入历史
                user_id = event.get_sender_id()
                self._add_user_history(user_id, {
                    "id": album_id,
                    "title": album_title,
                    "time": time.strftime("%Y-%m-%d %H:%M"),
                    "size_mb": round(size_mb, 1)
                })

            except Exception as e:
                failed_ids.append(album_id)
                yield event.plain_result(f"❌ [{idx+1}/{len(id_list)}] JM{album_id} 下载失败：{e}")

        # 汇总
        if failed_ids:
            yield event.plain_result(f"⚠️ 以下 {len(failed_ids)} 本下载失败：{', '.join(failed_ids)}")
        yield event.plain_result(f"📊 批量下载完成：成功 {success_count}/{len(id_list)} 本")

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

        # 解析后面的参数（页码/排序）
        extra = args[2:]
        for arg in extra:
            if arg.isdigit():
                qq_page = int(arg)
            elif arg in self.order_map:
                order_by = arg

        return keyword, qq_page, order_by

    async def _render_search_page(self, event, client, search_method, keyword, qq_page, order_by, title_prefix=""):
        """通用搜索结果渲染：分页 + 切片 + 输出"""
        PER_QQ_PAGE = self.per_page
        PER_WEB_PAGE = 80
        QQ_PAGES_PER_WEB_PAGE = PER_WEB_PAGE // PER_QQ_PAGE

        web_page = math.ceil(qq_page / QQ_PAGES_PER_WEB_PAGE)
        offset_in_web_page = (qq_page - 1) % QQ_PAGES_PER_WEB_PAGE * PER_QQ_PAGE

        order_name = self.order_map.get(order_by, order_by)
        yield event.plain_result(f"🔍 {title_prefix}{keyword}（{order_name}，第{qq_page}页）...")

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

            if not page_items:
                if total:
                    max_page = math.ceil(int(total) / PER_QQ_PAGE)
                    yield event.plain_result(f"第{qq_page}页没有更多结果了（共{total}条，最多{max_page}页）")
                else:
                    yield event.plain_result(f"第{qq_page}页没有更多结果了")
                return

            results = []
            for i, (aid, item_title, tags) in enumerate(page_items):
                global_index = (qq_page - 1) * PER_QQ_PAGE + i + 1
                item_title = self._safe_text(item_title, self.convert_title)
                tags = [self._safe_text(t, self.convert_tags) for t in tags]
                safe_tags = tags[:3]
                tag_str = f" [{', '.join(safe_tags)}]" if safe_tags else ""
                results.append(f"{global_index}. [JM{aid}] {item_title}{tag_str}")

            reply = f"{title_prefix}：{keyword}（{order_name}）\n"
            range_text = f"第{qq_page}页，第{start+1}-{start+len(page_items)}条"
            if total is not None:
                range_text += f"，共{total}条"
            reply += range_text + "\n\n" + "\n".join(results)
            reply += f"\n\n/jmv <id> 查详情，/jm <id> 下载"

            # 下一页提示
            next_cmd = f"/jms {keyword} {qq_page + 1}"
            if order_by != JmMagicConstants.ORDER_BY_LATEST:
                next_cmd += f" {order_by}"
            reply += f"\n下一页：{next_cmd}"

            yield event.plain_result(reply)

        except Exception as e:
            yield event.plain_result(self._handle_jm_exception(e, "搜索"))

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
          -work    去掉原作
          -role    去掉角色
          -tag     去掉标签
          -desc    去掉简介
          -related 去掉相关推荐
          -pic     不发送封面
        示例：/jmv 123456 -role -work
        """
        if not self._check_permission(event):
            yield event.plain_result("本插件已关闭群聊使用，请私聊使用")
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result(
                "用法：/jmv <漫画id> [选项]\n"
                "选项：-author 去掉作者 | -work 去掉原作 | -role 去掉角色 | -tag 去掉标签\n"
                "      -desc 去掉简介 | -related 去掉相关推荐 | -pic 不发送封面\n"
                "示例：/jmv 350234 或 /jmv 350234 -role -work"
            )
            return

        album_id = args[1]
        options = set(args[2:])  # 后面的参数都是选项

        yield event.plain_result(f"🔍 查询 JM{album_id} ...")

        try:
            client = self._new_logged_client()
            album = await asyncio.to_thread(client.get_album_detail, album_id)

            chapters = list(album) if hasattr(album, '__iter__') else []
            authors = getattr(album, 'authors', [])
            works = getattr(album, 'works', [])
            actors = getattr(album, 'actors', [])
            tags = getattr(album, 'tags', [])
            page_count = getattr(album, 'page_count', 0) or 0
            comment_count = getattr(album, 'comment_count', 0) or 0
            description = getattr(album, 'description', '') or ''
            pub_date = getattr(album, 'pub_date', '') or ''
            update_date = getattr(album, 'update_date', '') or ''
            likes = getattr(album, 'likes', '') or ''
            views = getattr(album, 'views', '') or ''
            related_list = getattr(album, 'related_list', None) or []

            # API客户端 page_count 可能为0，从章节累加计算
            if page_count == 0 and chapters:
                page_count = sum(len(getattr(p, 'page_arr', []) or []) for p in chapters)

            lines = [
                f"📖 {self._safe_text(album.name, self.convert_title)}",
                f"🆔 JM{album_id}",
            ]

            if '-author' not in options:
                author_str = ', '.join(authors) if authors else '未知'
                author_str = self._safe_text(author_str, self.convert_author)
                lines.append(f"✍️ 作者：{author_str}")
            if '-work' not in options:
                work_str = ', '.join(works) if works else '无'
                work_str = self._safe_text(work_str, self.convert_title)
                lines.append(f"🎬 原作：{work_str}")
            if '-role' not in options:
                actor_str = ', '.join(actors[:3]) if actors else '无'
                actor_str = self._safe_text(actor_str, self.convert_tags)
                lines.append(f"👥 角色：{actor_str}")

            if '-tag' not in options:
                tags = [self._safe_text(t, self.convert_tags) for t in tags]
                if tags:
                    lines.append(f"🏷️ 标签：{', '.join(tags)}")

            # 简介
            if description and '-desc' not in options:
                desc = self._safe_text(description, self.convert_title)
                desc = desc.replace('\n', ' ').strip()[:150]
                if desc:
                    lines.append(f"📝 简介：{desc}")

            # 统计信息（API客户端update_date硬编码为'0'，过滤掉无效值）
            stats = []
            if likes:
                stats.append(f"❤️ {likes}")
            if views:
                stats.append(f"👁️ {views}")
            if page_count and page_count != 0:
                stats.append(f"📄 {page_count}页")
            if comment_count and comment_count != 0:
                stats.append(f"💬 {comment_count}条")
            if pub_date and pub_date not in ('0', '', None):
                stats.append(f"📅 {pub_date}")
            if stats:
                lines.append(" | ".join(stats))

            # 相关推荐（API客户端返回字典列表，含id/author/name/image）
            if related_list and '-related' not in options:
                lines.append("\n🔗 相关推荐：")
                for i, rel in enumerate(related_list[:5]):
                    if isinstance(rel, dict):
                        rel_id = rel.get('id', '')
                        rel_title = rel.get('name', '')
                        rel_author = rel.get('author', '')
                    elif isinstance(rel, (tuple, list)) and len(rel) >= 2:
                        rel_id, rel_title = rel[0], rel[1]
                        rel_author = ''
                    else:
                        rel_id, rel_title, rel_author = str(rel), str(rel), ''
                    if self.convert_title:
                        rel_title = self._to_simplified(rel_title)
                        rel_author = self._to_simplified(rel_author)
                    rel_title = self._safe_text(rel_title, False)
                    rel_author = self._safe_text(rel_author, False)
                    author_suffix = f"（{rel_author}）" if rel_author else ''
                    lines.append(f"  {i+1}. [JM{rel_id}] {rel_title[:30]}{author_suffix}")

            # 章节列表（最多显示10章，方便获取章节ID用于单章下载）
            if chapters:
                lines.append("\n📚 章节列表：")
                for i, photo in enumerate(chapters[:10]):
                    photo_title = self._safe_text(photo.name, self.convert_title)
                    lines.append(f"  {i+1}. [JM{photo.photo_id}] {photo_title[:40]}")
                if len(chapters) > 10:
                    lines.append(f"  ... 共{len(chapters)}章，仅显示前10章")
                lines.append(f"\n💡 /jmphoto <章节id> 下载单章，/jmlist {album_id} 查看全部章节，/jm {album_id} 下载全本")
            else:
                lines.append(f"\n发 /jm {album_id} 下载")

            reply = "\n".join(lines)
            yield event.plain_result(reply)

            # 下载封面并发送；-pic 不发送封面
            if '-pic' not in options:
                cover_path = self.data_dir / f"cover_{album_id}.jpg"
                try:
                    await asyncio.to_thread(client.download_album_cover, album_id, str(cover_path))
                    if cover_path.exists():
                        yield event.chain_result([
                            Comp.Image(file=str(cover_path))
                        ])
                        # 发完就删，不存着
                        cover_path.unlink(missing_ok=True)
                except Exception as e:
                    logger.warning(f"封面下载失败: {e}")

        except Exception as e:
            yield event.plain_result(self._handle_jm_exception(e, "查询"))

    # ========== /jmlist 章节列表 ==========
    @filter.command("jmlist")
    async def jm_chapter_list(self, event: AstrMessageEvent):
        """查看漫画章节列表，用法：/jmlist <漫画id> [页码]
        只发送章节文本，不发送封面；封面请用 /jmv <漫画id> 查看
        """
        if not self._check_permission(event):
            yield event.plain_result("本插件已关闭群聊使用，请私聊使用")
            return

        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result(
                "用法：/jmlist <漫画id> [页码]\n"
                "示例：/jmlist 123456\n"
                "      /jmlist 123456 2     查看第2页章节"
            )
            return

        album_id = self._extract_id(args[1]) or args[1]
        page_num = 1
        for token in args[2:]:
            if token.isdigit():
                page_num = max(1, int(token))

        yield event.plain_result(f"📚 获取 JM{album_id} 的章节列表...")

        try:
            client = self._new_logged_client()
            album = await asyncio.to_thread(client.get_album_detail, album_id)
            chapters = list(album) if hasattr(album, '__iter__') else []

            if not chapters:
                yield event.plain_result(
                    f"⚠️ JM{album_id} 没有章节列表。\n"
                    "如果这是章节ID，请用 /jmphoto <章节id> 下载；"
                    "想看所属漫画可以用 /jmv <章节id> 试着查一下。"
                )
                return

            page_size = 20
            total_pages = max(1, math.ceil(len(chapters) / page_size))
            if page_num > total_pages:
                yield event.plain_result(
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
            lines.append(f"💡 下载单章：/jmphoto <章节id>；下载全本：/jm {album_id}")
            text = "\n".join(lines)

            # 直接发送，便于捕获 QQ 的“消息内容违规”异常；再用日志兜底检测静默拦截
            mark = self._log_pos()
            try:
                await event.send(MessageChain(chain=[Comp.Plain(text)]))
            except Exception as send_err:
                violation = self._is_violation_error(send_err) or self._log_has_violation(mark)
                if violation:
                    yield event.plain_result(
                        "⚠️ 章节列表被 QQ 判定为消息内容违规，已拦截。\n"
                        "标题已做敏感词转拼音；可以换一页重试，或用 /jmv <漫画id> -pic 看详情。"
                    )
                else:
                    logger.warning(f"jmlist 发送失败: {send_err}")
                    yield event.plain_result(f"❌ 章节列表发送失败：{send_err}")
                return

            # 给平台发送/日志落盘留一点时间，再检查是否静默拦截
            await asyncio.sleep(1.5)
            if self._log_has_violation(mark):
                yield event.plain_result(
                    "⚠️ 日志检测到章节列表触发了“消息内容违规”，可能已被 QQ 拦截。\n"
                    "可以换一页重试，标题已做敏感词转拼音。"
                )
                return

        except Exception as e:
            yield event.plain_result(self._handle_jm_exception(e, "获取章节列表"))

    # ========== /jm 下载并发ZIP ==========
    @filter.command("jm")
    async def jm_download(self, event: AstrMessageEvent):
        """下载漫画并打包ZIP发到QQ，用法：/jm <id> 或 /jm help"""
        if not self._check_permission(event):
            yield event.plain_result("本插件已关闭群聊使用，请私聊使用")
            return
        args = event.message_str.split()

        # 不带参数或 help，输出帮助
        if len(args) < 2 or args[1].lower() == 'help':
            help_text = (
                "📖 漫画站插件 帮助 v0.6.4\n"
                "━━━━━━━━━━━━━━━\n"
                "🔍 搜索：\n"
                "  /jms 关键词 [页码] [排序]   全站搜索\n"
                "  /jma 作者名 [页码] [排序]   按作者搜索\n"
                "  /jmt 标签名 [页码] [排序]   按标签搜索\n"
                "  /jmr 角色名 [页码] [排序]   按角色搜索\n"
                "  /jmw 原作名 [页码] [排序]   按原作搜索\n"
                "━━━━━━━━━━━━━━━\n"
                "📖 详情：\n"
                "  /jmv <漫画id> [选项]       查详情(含章节列表/简介/相关推荐)\n"
                "    选项：-author -work -role -tag -desc -related -pic(不发封面)\n"
                "  /jmlist <漫画id> [页码]    查看全部章节（纯文本，不发封面）\n"
                "  /jmc <漫画id> [页码]        看评论(翻页/回复/剧透，超页自动提示)\n"
                "━━━━━━━━━━━━━━━\n"
                "⬇️ 下载：\n"
                "  /jm <漫画id>                下载并发ZIP(带进度，已下载自动复用缓存)\n"
                "  /jm id1,id2,id3             批量下载多本\n"
                "  /jmpdf <漫画id>             下载并发PDF\n"
                "  /jmphoto <章节id>           下载单个章节\n"
                "  /jmlong <漫画id>            下载并合并为长图PNG\n"
                "━━━━━━━━━━━━━━━\n"
                "⭐ 账号：\n"
                "  /jmcheckin                  每日签到领金币经验\n"
                "  /jmme                       用户信息面板(签到/收藏/进度)\n"
                "  /jmfav                      收藏夹文件夹列表\n"
                "  /jmfav <文件夹id>           查看指定收藏夹\n"
                "  /jmfav add <漫画id>         添加收藏\n"
                "  /jmfav del <漫画id>         移除收藏\n"
                "━━━━━━━━━━━━━━━\n"
                "📊 其他：\n"
                "  /jmrank day/week/month     排行榜\n"
                "  /jmhis                      下载历史\n"
                "    用法：/jmhis clear 清空历史 | /jmhis -all 查看全部成员(管理员)\n"
                "  /jmdomain                   查看可用域名\n"
                "  /jmupdate                   查看更新日志\n"
                "  /jmrand [关键词]            随机抽一个\n"
                "━━━━━━━━━━━━━━━\n"
                "📝 排序参数：mr(最新) mv(最多浏览) mp(最多页数) tf(最多收藏) tr(评分最高) md(最多评论)\n"
                "💡 搜索小提示：建议只打前几个关键词，打全容易因简繁差异漏结果\n"
                "💾 下载缓存：JM{{漫画id}}_{{标题}}，默认保留 {cache_days} 天\n"
                "⚙️ 当前客户端：{client_type}（插件配置可切换 api/html）\n"
                "📦 需 jmcomic 2.7.9+（requirements 会自动安装/升级）".format(
                    client_type=self.client_type.upper(), cache_days=self.cache_days
                )
            )
            yield event.plain_result(help_text)
            return

        album_id = args[1]

        # 支持批量下载：逗号分隔多个ID，如 /jm 123,456,789
        if ',' in album_id:
            async for msg in self._download_batch(event, album_id):
                yield msg
            return

        # 先找downloads里有没有已经下载好的漫画
        album_dir = self._find_album_dir(album_id)
        if album_dir is not None:
            yield event.plain_result(f"📦 找到已下载的文件，直接打包发送...")
            zip_name = f"{self._safe_filename(album_dir.name, 60)}.zip"
            zip_path = self.zip_base_dir / zip_name
            await asyncio.to_thread(self._zip_folder, album_dir, zip_path)
            size_mb = zip_path.stat().st_size / 1024 / 1024
            yield event.chain_result([
                Comp.Plain(f"📎 {zip_name}\n"),
                Comp.File(file=str(zip_path), name=zip_name)
            ])
            zip_path.unlink(missing_ok=True)
            return

        yield event.plain_result(f"⬇️ 开始下载 JM{album_id} ...")

        try:
            # 启动下载任务（使用带进度跟踪的下载器）
            download_task = asyncio.create_task(
                jmcomic.download_album_async(album_id, self.option, downloader=ProgressDownloader)
            )

            # 定期推送下载进度
            last_pct = -1
            while not download_task.done():
                await asyncio.sleep(3)
                prog = ProgressDownloader._progress.get(str(album_id))
                if prog and prog['total'] > 0 and not prog['finished']:
                    pct = int(prog['done'] / prog['total'] * 100)
                    if pct != last_pct and pct < 100:
                        last_pct = pct
                        yield event.plain_result(f"⏳ 下载进度：{pct}% ({prog['done']}/{prog['total']}张)")

            # 获取下载结果，按ID查找目录（不依赖album.save_path）
            result = download_task.result()
            album = result.detail

            album_dir = self._find_album_dir(album_id)
            if album_dir is None:
                yield event.plain_result("⚠️ 下载完成但找不到漫画目录")
                return

            yield event.plain_result(f"📦 正在打包ZIP ...")
            safe_album_title = self._safe_text(album.name, self.convert_title)
            zip_name = f"JM{album_id}_{self._safe_filename(safe_album_title, 30)}.zip"
            zip_path = self.zip_base_dir / zip_name

            await asyncio.to_thread(self._zip_folder, album_dir, zip_path)
            size_mb = zip_path.stat().st_size / 1024 / 1024

            yield event.plain_result(
                f"✅ 完成！耗时 {result.duration:.1f}s，大小 {size_mb:.1f}MB\n"
                f"正在发送文件..."
            )
            yield event.chain_result([
                Comp.Plain(f"📎 {zip_name}\n"),
                Comp.File(file=str(zip_path), name=zip_name)
            ])

            # 发送完保留downloads里的原图，下次直接打包，不删
            # 删除zips里的临时zip
            zip_path.unlink(missing_ok=True)

            # 写入下载历史
            user_id = event.get_sender_id()
            total_count = self._add_user_history(user_id, {
                "id": album_id,
                "title": album.name,
                "time": time.strftime("%Y-%m-%d %H:%M"),
                "size_mb": round(size_mb, 1)
            })

            # 调侃吐槽
            import random
            teases = []
            if total_count == 3:
                teases = ["兄弟，今天已经下3本了，悠着点"]
            elif total_count == 5:
                teases = ["🚨 5本了！再看下去要樯橹灰飞烟灭了", "🚨 提醒：5本了，身体要紧啊兄弟"]
            elif total_count == 7:
                teases = ["差不多得了，出去看看太阳吧，别一直盯着屏幕"]
            elif total_count == 10:
                teases = ["🚨 警告：10本！你这频率比我上班打卡都勤", "🚨 10本了！再看下去腰都要坐坏了"]
            elif total_count == 15:
                teases = ["你这下载量，网站管理员都要注意到你了"]
            elif total_count == 20:
                teases = ["🚨 严重警告：20本！JM官方都要给你发终身VIP了", "🚨 20本了！你这是准备把整个网站搬回家？"]
            elif total_count == 30:
                teases = ["你这用量，键盘都要被你磨包浆了"]
            elif total_count == 50:
                teases = ["🚨 50本了兄弟！建议今天到此为止，明天再看"]
            # 内置调侃语录
            if self.tease_enabled and teases:
                yield event.plain_result(random.choice(teases))

        except Exception as e:
            yield event.plain_result(self._handle_jm_exception(e, "下载"))

    # ========== /jmrand 随机漫画 ==========
    @filter.command("jmrand")
    async def jm_random(self, event: AstrMessageEvent):
        """随机抽一个漫画，用法：/jmrand [标签名]
        不填标签就从日榜随机，填了就从指定标签里随机
        """
        args = event.message_str.split()
        tag_name = " ".join(args[1:]) if len(args) > 1 else None

        if tag_name:
            yield event.plain_result(f"🎲 正在从「{tag_name}」标签随机抽一个...")
        else:
            yield event.plain_result("🎲 正在从日榜随机抽一个漫画...")

        try:
            import random
            client = self._new_logged_client()

            if tag_name:
                # 全站搜索，关键词可以是标题/标签/作者
                page = await asyncio.to_thread(client.search_site, tag_name, 1)
            else:
                # 从日榜第1页随机挑一个
                page = await asyncio.to_thread(client.day_ranking, 1)

            items = list(page.iter_id_title_tag())
            if not items:
                yield event.plain_result("没抽到，再试一次")
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
            yield event.plain_result(
                f"🎲 抽到了！\n\n"
                f"📖 {title}\n"
                f"🆔 JM{album_id}"
                f"{tag_str}\n"
                f"📄 {len(chapters)}章 / {page_count}页\n\n"
                f"发 /jm {album_id} 下载"
            )

        except Exception as e:
            yield event.plain_result(self._handle_jm_exception(e, "随机抽漫"))

    # ========== /jms 全站搜索 ==========
    @filter.command("jms")
    async def jm_search(self, event: AstrMessageEvent):
        """全站搜索，用法：/jms <关键词> [页码] [排序]"""
        if not self._check_permission(event):
            yield event.plain_result("本插件已关闭群聊使用，请私聊使用")
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result(
                "用法：\n"
                "  /jms <关键词>\n"
                "  /jms <关键词> 2\n"
                "  /jms <关键词> mv   (mv=最多浏览, tf=最多收藏)"
            )
            return

        keyword, qq_page, order_by = self._parse_search_args(args)
        client = self._new_logged_client()

        async for result in self._render_search_page(
            event, client, client.search_site, keyword, qq_page, order_by, "全站搜索"
        ):
            yield result

    # ========== /jma 按作者搜索 ==========
    @filter.command("jma")
    async def jm_search_author(self, event: AstrMessageEvent):
        """按作者搜索，用法：/jma <作者名> [页码] [排序]"""
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法：/jma <作者名>")
            return

        keyword, qq_page, order_by = self._parse_search_args(args)
        client = self._new_logged_client()

        async for result in self._render_search_page(
            event, client, client.search_author, keyword, qq_page, order_by, "作者搜索"
        ):
            yield result

    # ========== /jmt 按标签搜索 ==========
    @filter.command("jmt")
    async def jm_search_tag(self, event: AstrMessageEvent):
        """按标签搜索，用法：/jmt <标签名> [页码] [排序]"""
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法：/jmt <标签名>")
            return

        keyword, qq_page, order_by = self._parse_search_args(args)
        client = self._new_logged_client()

        async for result in self._render_search_page(
            event, client, client.search_tag, keyword, qq_page, order_by, "标签搜索"
        ):
            yield result

    # ========== /jmr 按角色搜索 ==========
    @filter.command("jmr")
    async def jm_search_actor(self, event: AstrMessageEvent):
        """按角色/演员搜索，用法：/jmr <角色名> [页码] [排序]"""
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法：/jmr <角色名>")
            return

        keyword, qq_page, order_by = self._parse_search_args(args)
        client = self._new_logged_client()

        async for result in self._render_search_page(
            event, client, client.search_actor, keyword, qq_page, order_by, "角色搜索"
        ):
            yield result

    # ========== /jmw 按原作搜索 ==========
    @filter.command("jmw")
    async def jm_search_work(self, event: AstrMessageEvent):
        """按原作搜索，用法：/jmw <原作名> [页码] [排序]"""
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法：/jmw <原作名>")
            return

        keyword, qq_page, order_by = self._parse_search_args(args)
        client = self._new_logged_client()

        async for result in self._render_search_page(
            event, client, client.search_work, keyword, qq_page, order_by, "原作搜索"
        ):
            yield result

    # ========== /jmpdf 下载并发PDF ==========
    @filter.command("jmpdf")
    async def jm_download_pdf(self, event: AstrMessageEvent):
        """下载漫画并发PDF，用法：/jmpdf <id>"""
        if not self._check_permission(event):
            yield event.plain_result("本插件已关闭群聊使用，请私聊使用")
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法：/jmpdf <漫画id>")
            return

        album_id = args[1]

        # 先找downloads里有没有已经下载好的漫画
        album_dir = self._find_album_dir(album_id)
        if album_dir is not None:
            image_files = sorted([f for f in album_dir.rglob("*.jpg")] + [f for f in album_dir.rglob("*.png")])
            if image_files:
                yield event.plain_result(f"📄 找到已下载的文件，直接生成PDF...")
                from PIL import Image
                images = [Image.open(img_path).convert('RGB') for img_path in image_files]
                pdf_name = f"{self._safe_filename(album_dir.name, 60)}.pdf"
                pdf_path = self.zip_base_dir / pdf_name
                images[0].save(pdf_path, save_all=True, append_images=images[1:], resolution=100.0)
                size_mb = pdf_path.stat().st_size / 1024 / 1024
                yield event.chain_result([
                    Comp.Plain(f"📎 {pdf_name}\n"),
                    Comp.File(file=str(pdf_path), name=pdf_name)
                ])
                pdf_path.unlink(missing_ok=True)
                return

        yield event.plain_result(f"⬇️ 开始下载 JM{album_id} 并生成PDF ...")

        try:
            # 启动下载任务（使用带进度跟踪的下载器）
            download_task = asyncio.create_task(
                jmcomic.download_album_async(album_id, self.option, downloader=ProgressDownloader)
            )

            # 定期推送下载进度
            last_pct = -1
            while not download_task.done():
                await asyncio.sleep(3)
                prog = ProgressDownloader._progress.get(str(album_id))
                if prog and prog['total'] > 0 and not prog['finished']:
                    pct = int(prog['done'] / prog['total'] * 100)
                    if pct != last_pct and pct < 100:
                        last_pct = pct
                        yield event.plain_result(f"⏳ 下载进度：{pct}% ({prog['done']}/{prog['total']}张)")

            # 获取下载结果，按ID查找目录
            result = download_task.result()
            album = result.detail
            album_dir = self._find_album_dir(album_id)
            if album_dir is None:
                yield event.plain_result("⚠️ 下载完成但找不到漫画目录")
                return

            # 找所有图片，按顺序排序
            image_files = sorted([f for f in album_dir.rglob("*.jpg")] + [f for f in album_dir.rglob("*.png")])
            if not image_files:
                yield event.plain_result("⚠️ 下载完成但找不到图片")
                return

            yield event.plain_result(f"📄 正在生成PDF（共{len(image_files)}页）...")

            # 生成PDF
            from PIL import Image
            images = []
            for img_path in image_files:
                img = Image.open(img_path).convert('RGB')
                images.append(img)

            pdf_name = f"JM{album_id}_{self._safe_filename(album.name, 30)}.pdf"
            pdf_path = self.zip_base_dir / pdf_name

            # 保存PDF
            if images:
                images[0].save(
                    pdf_path,
                    save_all=True,
                    append_images=images[1:],
                    resolution=100.0
                )

            size_mb = pdf_path.stat().st_size / 1024 / 1024

            yield event.plain_result(
                f"✅ PDF生成完成！大小 {size_mb:.1f}MB\n"
                f"正在发送..."
            )
            yield event.chain_result([
                Comp.Plain(f"📎 {pdf_name}\n"),
                Comp.File(file=str(pdf_path), name=pdf_name)
            ])

            # 保留downloads里的原图，下次直接用，删除临时PDF
            pdf_path.unlink(missing_ok=True)

            # 写入下载历史
            user_id = event.get_sender_id()
            total_count = self._add_user_history(user_id, {
                "id": album_id,
                "title": album.name,
                "time": time.strftime("%Y-%m-%d %H:%M"),
                "size_mb": round(size_mb, 1)
            })

            # 调侃吐槽
            import random
            teases = []
            if total_count == 3:
                teases = ["兄弟，今天已经下3本了，悠着点"]
            elif total_count == 5:
                teases = ["🚨 5本了！再看下去要樯橹灰飞烟灭了", "🚨 提醒：5本了，身体要紧啊兄弟"]
            elif total_count == 7:
                teases = ["差不多得了，出去看看太阳吧，别一直盯着屏幕"]
            elif total_count == 10:
                teases = ["🚨 警告：10本！你这频率比我上班打卡都勤", "🚨 10本了！再看下去腰都要坐坏了"]
            elif total_count == 15:
                teases = ["你这下载量，网站管理员都要注意到你了"]
            elif total_count == 20:
                teases = ["🚨 严重警告：20本！JM官方都要给你发终身VIP了", "🚨 20本了！你这是准备把整个网站搬回家？"]
            elif total_count == 30:
                teases = ["你这用量，键盘都要被你磨包浆了"]
            elif total_count == 50:
                teases = ["🚨 50本了兄弟！建议今天到此为止，明天再看"]
            # 内置调侃语录
            if self.tease_enabled and teases:
                yield event.plain_result(random.choice(teases))

        except Exception as e:
            yield event.plain_result(self._handle_jm_exception(e, "PDF下载"))

    # ========== /jmphoto 单章节下载 ==========
    @filter.command("jmphoto")
    async def jm_download_photo(self, event: AstrMessageEvent):
        """下载单个章节，用法：/jmphoto <章节id>
        章节ID可在 /jmv 详情中查看章节列表获取
        """
        if not self._check_permission(event):
            yield event.plain_result("本插件已关闭群聊使用，请私聊使用")
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result(
                "用法：/jmphoto <章节id>\n"
                "章节ID可通过 /jmv <漫画id> 查看章节列表获取"
            )
            return

        photo_id = args[1]
        yield event.plain_result(f"⬇️ 开始下载章节 JM{photo_id} ...")

        try:
            # 下载单章节
            result = await jmcomic.download_photo_async(photo_id, self.option)
            photo = result.detail
            photo_dir = Path(photo.save_path)

            if not photo_dir.exists():
                yield event.plain_result("⚠️ 下载完成但找不到目录")
                return

            # 打包ZIP
            zip_name = f"JM{photo_id}_{self._safe_filename(photo.name, 30)}.zip"
            zip_path = self.zip_base_dir / zip_name

            await asyncio.to_thread(self._zip_folder, photo_dir, zip_path)
            size_mb = zip_path.stat().st_size / 1024 / 1024

            yield event.plain_result(
                f"✅ 章节下载完成！耗时 {result.duration:.1f}s，大小 {size_mb:.1f}MB\n"
                f"正在发送文件..."
            )
            yield event.chain_result([
                Comp.Plain(f"📎 {zip_name}\n"),
                Comp.File(file=str(zip_path), name=zip_name)
            ])
            zip_path.unlink(missing_ok=True)

        except Exception as e:
            yield event.plain_result(self._handle_jm_exception(e, "章节下载"))

    # ========== /jmlong 长图导出 ==========
    @filter.command("jmlong")
    async def jm_long_image(self, event: AstrMessageEvent):
        """下载漫画并合并为长图PNG发送，用法：/jmlong <id>
        手机上直接看图比ZIP/PDF更方便
        """
        if not self._check_permission(event):
            yield event.plain_result("本插件已关闭群聊使用，请私聊使用")
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法：/jmlong <漫画id>")
            return

        album_id = args[1]

        # 先找已下载的缓存
        album_dir = self._find_album_dir(album_id)

        if album_dir is not None:
            yield event.plain_result(f"🖼️ 找到已下载的文件，正在生成长图...")
        else:
            yield event.plain_result(f"⬇️ 开始下载 JM{album_id} 并生成长图...")
            try:
                result = await jmcomic.download_album_async(album_id, self.option, downloader=ProgressDownloader)
                album_dir = self._find_album_dir(album_id)
            except Exception as e:
                yield event.plain_result(self._handle_jm_exception(e, "下载"))
                return

        # 安全检查
        if album_dir is None:
            yield event.plain_result("❌ 下载完成但找不到漫画目录")
            return

        try:
            # 找所有图片，按顺序排序（只在当前目录下找，不递归到其他漫画）
            image_files = sorted(
                [f for f in album_dir.glob("*.jpg")] +
                [f for f in album_dir.glob("*.png")] +
                [f for f in album_dir.glob("*.webp")]
            )
            # 如果当前目录没有图片，可能有章节子目录
            if not image_files:
                for sub in sorted(album_dir.iterdir()):
                    if sub.is_dir():
                        image_files.extend(sorted(
                            [f for f in sub.glob("*.jpg")] +
                            [f for f in sub.glob("*.png")] +
                            [f for f in sub.glob("*.webp")]
                        ))
            if not image_files:
                yield event.plain_result("⚠️ 找不到图片文件")
                return

            yield event.plain_result(f"🖼️ 正在合并 {len(image_files)} 张图片为长图...")

            from PIL import Image
            images = []
            for img_path in image_files:
                try:
                    img = Image.open(img_path).convert('RGB')
                    images.append(img)
                except Exception:
                    continue

            if not images:
                yield event.plain_result("⚠️ 图片打开失败")
                return

            # 统一宽度，竖向拼接
            min_width = min(img.width for img in images)
            total_height = 0
            resized = []
            for img in images:
                if img.width > min_width:
                    new_height = int(img.height * min_width / img.width)
                    img = img.resize((min_width, new_height), Image.LANCZOS)
                resized.append(img)
                total_height += img.height

            long_img = Image.new('RGB', (min_width, total_height))
            y_offset = 0
            for img in resized:
                long_img.paste(img, (0, y_offset))
                y_offset += img.height

            # 保存长图
            long_name = f"JM{album_id}_long.png"
            long_path = self.zip_base_dir / long_name
            long_img.save(long_path, 'PNG', optimize=True)
            size_mb = long_path.stat().st_size / 1024 / 1024

            for img in images:
                img.close()
            long_img.close()

            yield event.plain_result(f"✅ 长图生成完成！{min_width}x{total_height}，{size_mb:.1f}MB，发送中...")
            yield event.chain_result([
                Comp.Image(file=str(long_path))
            ])
            long_path.unlink(missing_ok=True)

        except Exception as e:
            yield event.plain_result(self._handle_jm_exception(e, "生成长图"))

    def _load_checkin_state(self):
        """读取本地签到记录（用于接口数据延迟时兜底）"""
        try:
            data = json.loads(self.checkin_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_checkin_state(self, streak=None, signed_count=None):
        """记录本次签到日期/连续天数，第二天自动失效"""
        state = {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'streak': streak,
            'signed_count': signed_count,
        }
        try:
            self.checkin_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.debug(f"保存签到状态失败: {e}")

    def _local_streak(self, today_signed=False):
        """接口日历缺少连续天数时，用本地签到记录兜底。
        连续签到跨月时 API 的 record 只给当月，必须靠本地记录续上。
        """
        state = self._load_checkin_state()
        streak = state.get('streak')
        state_date = state.get('date')
        if streak is None or not state_date:
            return None
        try:
            streak = int(streak)
        except (TypeError, ValueError):
            return None
        today = datetime.now().strftime('%Y-%m-%d')
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        if state_date == today:
            return streak
        if state_date == yesterday:
            return streak + (1 if today_signed else 0)
        return None

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
        try:
            client = self._new_logged_client()

            # jmcomic 2.7.5 及以下版本不支持签到接口
            jm_version = getattr(jmcomic, '__version__', '未知')
            if not hasattr(client, 'get_daily') or not hasattr(client, 'daily_checkin'):
                yield event.plain_result(
                    f"⚠️ 当前 jmcomic 版本（{jm_version}）不支持每日签到功能\n"
                    f"需要 jmcomic >= 2.7.7（推荐升级到 2.7.9），请升级后重启 AstrBot：\n"
                    f"  D:\\Program Files\\AstrBot\\backend\\python\\python.exe -m pip install -U jmcomic"
                )
                return

            # 先获取签到信息
            yield event.plain_result("📅 正在获取签到信息...")
            daily = await asyncio.to_thread(client.get_daily)
            info = self._parse_daily_info(daily)

            # 执行签到
            yield event.plain_result("✅ 正在签到...")
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

            yield event.plain_result(reply)

        except Exception as e:
            yield event.plain_result(self._handle_jm_exception(e, "签到"))

    # ========== /jmme 用户面板 ==========
    @filter.command("jmme")
    async def jm_user_info(self, event: AstrMessageEvent):
        """查看用户信息面板，用法：/jmme"""
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
                yield event.plain_result("⚠️ 未配置登录账号，请在插件配置中填写账号密码")
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

            yield event.plain_result(reply)

        except Exception as e:
            yield event.plain_result(self._handle_jm_exception(e, "获取用户信息"))

    # ========== /jmrank 排行榜 ==========
    @filter.command("jmrank")
    async def jm_ranking(self, event: AstrMessageEvent):
        """排行榜，用法：/jmrank day|week|month [页码]"""
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result(
                "用法：\n"
                "  /jmrank day    日榜\n"
                "  /jmrank week   周榜\n"
                "  /jmrank month  月榜"
            )
            return

        rank_type = args[1].lower()
        qq_page = int(args[2]) if len(args) >= 3 else 1

        rank_map = {
            'day': ('日榜', 'day_ranking'),
            'week': ('周榜', 'week_ranking'),
            'month': ('月榜', 'month_ranking'),
        }

        if rank_type not in rank_map:
            yield event.plain_result("类型只能是 day / week / month")
            return

        rank_name, method_name = rank_map[rank_type]
        yield event.plain_result(f"🏆 获取{rank_name} 第{qq_page}页...")

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
                yield event.plain_result("没有更多了")
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
            yield event.plain_result(reply)

        except Exception as e:
            yield event.plain_result(self._handle_jm_exception(e, "获取排行榜"))

    # ========== /jmhis 下载历史 ==========
    @filter.command("jmhis")
    async def jm_history(self, event: AstrMessageEvent):
        """查看下载历史，用法：/jmhis 或 /jmhis clear 清空历史"""
        args = event.message_str.split()
        user_id = event.get_sender_id()

        if len(args) >= 2 and args[1].lower() == "clear":
            data = self._load_history()
            if user_id in data:
                del data[user_id]
            self._save_history(data)
            yield event.plain_result("🧹 你的下载历史已清空")
            return

        # /jmhis -all：管理员查看所有成员的历史
        if len(args) >= 2 and args[1].lower() in ("-all", "all"):
            is_admin = event.is_admin() if hasattr(event, 'is_admin') else getattr(event, 'role', '') == 'admin'
            if not is_admin:
                yield event.plain_result("⚠️ 只有管理员可以使用 /jmhis -all")
                return
            data = self._load_history()
            if not data:
                yield event.plain_result("还没有任何下载记录")
                return
            lines = []
            total_records = 0
            for uid, items in data.items():
                if not items:
                    continue
                total_records += len(items)
                lines.append(f"👤 用户 {uid}（{len(items)}条）")
                for item in items[:5]:
                    title = self._safe_text(item.get('title', ''), self.convert_title)
                    lines.append(
                        f"  [JM{item.get('id', '')}] {title[:30]}  "
                        f"({item.get('size_mb', '?')}MB)  {item.get('time', '')}"
                    )
                if len(items) > 5:
                    lines.append(f"  ... 还有{len(items) - 5}条")
                lines.append("")
            reply = f"📜 全部成员下载历史（{len(data)}人，共{total_records}条）\n\n" + "\n".join(lines)
            yield event.plain_result(reply[:3500])
            return

        history = self._get_user_history(user_id)
        if not history:
            yield event.plain_result("还没有下载记录")
            return

        results = []
        for i, item in enumerate(history[:20]):
            title = self._safe_text(item.get('title', ''), self.convert_title)
            results.append(
                f"{i+1}. [JM{item.get('id', '')}] {title[:30]}  "
                f"({item.get('size_mb', '?')}MB)  {item.get('time', '')}"
            )

        reply = "📜 下载历史（最近20条）：\n\n" + "\n".join(results)
        reply += "\n\n/jmhis clear 清空历史"
        yield event.plain_result(reply)

    # ========== /jmc 评论查看 ==========
    @filter.command("jmc")
    async def jm_comments(self, event: AstrMessageEvent):
        """查看评论，用法：/jmc <id> [页码]
        支持翻页，展示回复楼、剧透标记、点赞数
        """
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法：/jmc <漫画id> [页码]\n示例：/jmc 123456 或 /jmc 123456 2")
            return

        album_id = args[1]
        page_num = int(args[2]) if len(args) >= 3 and args[2].isdigit() else 1

        # HTML 客户端不支持评论翻页，最多10条
        if page_num > 1 and self.client_type != 'api':
            yield event.plain_result("⚠️ 评论翻页仅支持 API 客户端，请在插件配置中将客户端类型改为 api")
            return

        yield event.plain_result(f"💬 获取 JM{album_id} 的评论（第{page_num}页）...")

        try:
            client = self._new_logged_client()

            # 超页检测：如果已知总页数且超过，直接提示（避免API返回重复内容）
            comment_cache_key = f"comment_pages_{album_id}"
            known_max_page = getattr(self, '_comment_page_cache', {}).get(comment_cache_key)
            if known_max_page and page_num > known_max_page:
                yield event.plain_result(f"⚠️ JM{album_id} 评论共 {known_max_page} 页，已到最后一页")
                return

            page = await asyncio.to_thread(client.album_pagination, album_id, page=page_num)

            # 先拿到总页数并缓存（API 的 page_count 由 total 计算，最多500页）
            total = getattr(page, 'total', None)
            page_count = getattr(page, 'page_count', None)
            if page_count:
                if not hasattr(self, '_comment_page_cache'):
                    self._comment_page_cache = {}
                self._comment_page_cache[comment_cache_key] = page_count

            # 请求页码超过总页数时，上游会重复返回第一页内容，这里直接拦截
            if page_count is not None and page_num > page_count:
                yield event.plain_result(
                    f"⚠️ JM{album_id} 评论共 {page_count} 页，已到最后一页\n"
                    f"🔙 回到第1页：/jmc {album_id} 1"
                )
                return

            comments = list(page)
            if not comments:
                if page_num > 1:
                    yield event.plain_result(
                        f"⚠️ 第{page_num}页没有评论，已经到最后一页了\n"
                        f"🔙 上一页：/jmc {album_id} {page_num - 1}"
                    )
                else:
                    yield event.plain_result("还没有评论")
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

            yield event.plain_result(reply[:3500])

        except Exception as e:
            yield event.plain_result(self._handle_jm_exception(e, "获取评论"))

    # ========== /jmdomain 查看可用域名 ==========
    @filter.command("jmdomain")
    async def jm_domain(self, event: AstrMessageEvent):
        """查看 API 与 HTML 两套可用域名"""
        yield event.plain_result("🌐 正在获取可用域名...")
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

            yield event.plain_result(reply)
        except Exception as e:
            yield event.plain_result(self._handle_jm_exception(e, "获取域名"))

    # ========== /jmupdate 查看更新日志 ==========
    @filter.command("jmupdate")
    async def jm_changelog(self, event: AstrMessageEvent):
        """查看插件更新日志"""
        changelog_path = Path(__file__).parent / "CHANGELOG.md"
        if not changelog_path.exists():
            yield event.plain_result("❌ 更新日志文件不存在")
            return
        content = changelog_path.read_text(encoding="utf-8")
        # 只发最近两个版本
        parts = content.split("---\n\n## ")
        reply = parts[0]
        yield event.plain_result(f"📝 插件更新日志：\n\n{reply[:1500]}")

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

    @filter.command("jmfav")
    async def jm_favorite(self, event: AstrMessageEvent):
        """收藏夹功能：
        /jmfav → 查看收藏夹文件夹列表
        /jmfav <文件夹id> → 查看文件夹里的内容
        /jmfav add <漫画id> → 把漫画加入收藏夹
        /jmfav del <漫画id> → 从收藏夹移除
        """
        args = event.message_str.split()

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
                yield event.plain_result("⚠️ 未配置登录账号，无法使用收藏夹功能")
                return

            # /jmfav add <id> — 添加收藏（仅API客户端支持）
            if len(args) >= 3 and args[1].lower() == 'add':
                if self.client_type != 'api':
                    yield event.plain_result("⚠️ 添加收藏仅支持 API 客户端，请在插件配置中将客户端类型改为 api")
                    return
                album_id = args[2]
                yield event.plain_result(f"⭐ 正在检查 JM{album_id} 的收藏状态...")
                is_fav, _album = await self._favorite_state(client, album_id)
                if is_fav:
                    yield event.plain_result(f"ℹ️ JM{album_id} 已经在收藏夹里了")
                else:
                    await asyncio.to_thread(client.add_favorite_album, album_id, folder_id='0')
                    self._clear_client_cache(client)
                    yield event.plain_result(f"✅ 已收藏 JM{album_id}")
                try:
                    yield event.plain_result(await self._render_favorites(client, '0'))
                except Exception as list_err:
                    logger.warning(f"刷新收藏夹失败: {list_err}")
                return

            # /jmfav del <id> — 删除收藏（仅API客户端支持，且需jmcomic较新版本）
            if len(args) >= 3 and args[1].lower() == 'del':
                if self.client_type != 'api':
                    yield event.plain_result("⚠️ 移除收藏仅支持 API 客户端，请在插件配置中将客户端类型改为 api")
                    return
                if not hasattr(client, 'delete_favorite_album'):
                    yield event.plain_result("⚠️ 当前 jmcomic 版本不支持移除收藏，请升级 jmcomic 到最新版本")
                    return
                album_id = args[2]
                yield event.plain_result(f"🗑️ 正在检查 JM{album_id} 的收藏状态...")
                is_fav, _album = await self._favorite_state(client, album_id)
                if not is_fav:
                    yield event.plain_result(f"ℹ️ JM{album_id} 本来就不在收藏夹里，无需移除")
                else:
                    await asyncio.to_thread(client.delete_favorite_album, album_id, folder_id='0')
                    self._clear_client_cache(client)
                    yield event.plain_result(f"✅ 已移除收藏 JM{album_id}")
                try:
                    yield event.plain_result(await self._render_favorites(client, '0'))
                except Exception as list_err:
                    logger.warning(f"刷新收藏夹失败: {list_err}")
                return

            # /jmfav <folder_id> — 查看指定文件夹内容
            if len(args) >= 2 and args[1].isdigit():
                folder_id = args[1]
                yield event.plain_result(f"⭐ 获取收藏夹 [ID:{folder_id}] 的内容...")
                yield event.plain_result(
                    await self._render_favorites(
                        client, folder_id, f"⭐ 收藏夹 [ID:{folder_id}]", 20
                    )
                )
                return

            # /jmfav — 列出收藏夹文件夹
            yield event.plain_result("⭐ 获取我的收藏夹列表...")
            page = await asyncio.to_thread(client.favorite_folder, 1)

            # 列出所有收藏夹文件夹
            folders = list(page.iter_folder_id_name())
            if not folders:
                # 如果没有文件夹列表，直接显示全部收藏
                yield event.plain_result(
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
            yield event.plain_result(reply)

        except Exception as e:
            yield event.plain_result(self._handle_jm_exception(e, "收藏夹操作"))

    async def terminate(self):
        pass

"""插件配置：把 AstrBotConfig 解析成只读的 PluginSettings，并构建 jmcomic option。"""

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jmcomic
import yaml
from astrbot.api import logger
from jmcomic import JmModuleConfig

from .downloader import ADVANCED_RETRY_CONFIG


ORDER_MAP = {
    'mr': '最新',
    'mv': '最多浏览',
    'mp': '最多页数',
    'tf': '最多收藏',
    'tr': '评分最高',
    'md': '最多评论',
}


def _split_set(raw: Any) -> set[str]:
    """把逗号/中文逗号/空白分隔的配置串拆成集合。"""
    text = str(raw or "")
    text = (
        text.replace("，", ",").replace(" ", ",")
        .replace("\n", ",").replace("\t", ",")
    )
    return {item.strip() for item in text.split(",") if item.strip()}


def _parse_sensitive_map(raw: Any) -> dict[str, str]:
    """解析用户自定义敏感词：每行 `词=拼音`。"""
    result: dict[str, str] = {}
    for line in str(raw or "").strip().split("\n"):
        line = line.strip()
        if "=" not in line:
            continue
        word, pinyin = line.split("=", 1)
        word = word.strip()
        pinyin = pinyin.strip()
        if word and pinyin:
            result[word] = pinyin
    return result


def _build_option(
    *,
    client_type: str,
    custom_domains: list[str],
    username: str,
    password: str,
    photo_threads: int,
    image_threads: int,
    use_proxy: bool,
    proxy_url: str,
    download_base_dir: Path,
):
    """按配置动态生成 jmcomic option。"""
    # 根据用户配置选择客户端类型：
    #   api  = API客户端（移动端接口），功能全：收藏添加、评论翻页、签到、用户信息，不支持分类筛选
    #   html = HTML客户端（网页端接口），支持分类筛选，但收藏添加报错、评论不翻页、总数解析不出
    if client_type == "api":
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
    proxy_map = {"http": proxy_url, "https": proxy_url} if use_proxy else {}

    option_dict = {
        "log": False,
        "client": {
            "impl": client_type,
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
            # 目录保留 ID + 网站话数，标题写入 metadata.json，避免长标题
            # 触发 Windows MAX_PATH；结构：JM{漫画id}/P{章节id}_第N话/图片
            "rule": "Bd/JM{Aid}/P{Pid}_第{Palbum_index}话",
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
    option_str = yaml.dump(option_dict, allow_unicode=True)
    option = jmcomic.create_option_by_str(option_str)
    option.dir_rule.base_dir = str(download_base_dir)
    return option


@dataclass(frozen=True)
class PluginSettings:
    """初始化后只读的插件配置快照。"""

    plugin_dir: Path
    data_dir: Path
    download_base_dir: Path
    zip_base_dir: Path
    send_state_file: Path
    history_file: Path
    checkin_file: Path
    subscription_file: Path

    username: str
    password: str
    custom_domains: list[str]
    photo_threads: int
    image_threads: int
    per_page: int
    allow_group: bool
    enabled_groups: set[str]
    numeric_search_as_album: bool
    subscribe_interval: int
    convert_enabled: bool
    convert_title: bool
    convert_tags: bool
    convert_author: bool
    convert_comment: bool
    sensitive_filter: bool
    tease_enabled: bool
    cache_days: int
    comment_count: int
    history_max: int
    dual_search: bool

    owner_ids: set[str]
    daily_file_limit: int
    hourly_file_limit: int
    send_interval: float
    zip_group_size: int
    pdf_page_limit: int
    zip_page_limit: int
    page_turn_timeout: int
    page_prefetch_pages: int

    use_proxy: bool
    proxy_url: str
    client_type: str
    custom_sensitive_map: dict[str, str] = field(default_factory=dict)

    option: Any = None
    order_map: dict[str, str] = field(default_factory=lambda: dict(ORDER_MAP))

    @classmethod
    def from_config(cls, config: Any, plugin_dir: Path) -> "PluginSettings":
        """从 AstrBot 配置解析设置。

        plugin_dir 必须由调用方（main.py）传入插件根目录，不能用本文件的
        __file__ 推算，否则 data_dir 会算错两级。
        """
        config = config or {}
        plugin_dir = Path(plugin_dir)

        # 路径配置：放到 plugin_data 下，插件更新不会丢
        # 插件目录: .../data/plugins/astrbot_plugin_jmcomic/
        # 往上两级到 data/，再进 plugin_data/astrbot_plugin_jmcomic/
        data_dir = plugin_dir.parent.parent / "plugin_data" / "astrbot_plugin_jmcomic"
        download_base_dir = data_dir / "downloads"
        zip_base_dir = data_dir / "zips"

        username = config.get("username", "")
        password = config.get("password", "")
        domain_str = config.get("domain", "")
        # 支持多个域名，逗号分隔
        custom_domains = [d.strip() for d in domain_str.split(",") if d.strip()]
        photo_threads = int(config.get("photo_threads", 2))
        image_threads = int(config.get("image_threads", 5))

        # 客户端类型：api（默认，功能全）或 html（支持分类筛选）
        client_type = config.get("client_type", "api").lower()
        if client_type not in ("api", "html"):
            client_type = "api"

        # 代理：use_proxy=false 时显式传空 dict，避免系统代理环境变量干扰
        proxy_url = str(config.get("proxy_url", "") or "").strip()
        use_proxy = bool(config.get("use_proxy", False)) and bool(proxy_url)
        if bool(config.get("use_proxy", False)) and not proxy_url:
            logger.warning("已开启 use_proxy，但 proxy_url 为空，本次按直连处理")

        # ZIP 按章数合并；PDF 按页数合并，两者互不影响。
        # 旧配置 pdf_group_size 仅作为 ZIP 合并章数的兼容回退。
        configured_group = int(
            config.get("zip_group_size", config.get("pdf_group_size", 5)) or 0
        )
        configured_pages = int(config.get("pdf_page_limit", 100) or 0)
        # ZIP 单章页数上限，0 表示不按页数切分，只按章数合并
        configured_zip_pages = int(config.get("zip_page_limit", 300) or 0)

        option = _build_option(
            client_type=client_type,
            custom_domains=custom_domains,
            username=username,
            password=password,
            photo_threads=photo_threads,
            image_threads=image_threads,
            use_proxy=use_proxy,
            proxy_url=proxy_url,
            download_base_dir=download_base_dir,
        )

        return cls(
            plugin_dir=plugin_dir,
            data_dir=data_dir,
            download_base_dir=download_base_dir,
            zip_base_dir=zip_base_dir,
            send_state_file=data_dir / "send_limits.json",
            history_file=data_dir / "history.json",
            checkin_file=data_dir / "checkin_state.json",
            subscription_file=data_dir / "subscriptions.json",
            username=username,
            password=password,
            custom_domains=custom_domains,
            photo_threads=photo_threads,
            image_threads=image_threads,
            per_page=int(config.get("per_page", 10)),
            allow_group=config.get("allow_group", True),
            # 群白名单：逗号分隔的群号；为空时沿用 allow_group 的全局开关
            enabled_groups=_split_set(config.get("enabled_groups", "")),
            numeric_search_as_album=bool(config.get("numeric_search_as_album", False)),
            # 订阅更新检查间隔（秒），0 表示关闭后台检查；命令仍可正常增删订阅
            subscribe_interval=max(0, int(config.get("subscribe_interval", 3600) or 0)),
            convert_enabled=config.get("convert_enabled", True),
            convert_title=config.get("convert_title", True),
            convert_tags=config.get("convert_tags", True),
            convert_author=config.get("convert_author", True),
            convert_comment=config.get("convert_comment", True),
            sensitive_filter=config.get("sensitive_filter", True),
            tease_enabled=config.get("tease_enabled", False),
            cache_days=int(config.get("cache_days", 7)),
            comment_count=int(config.get("comment_count", 10)),
            history_max=max(0, int(config.get("history_max", 0) or 0)),
            dual_search=config.get("dual_search", True),
            # 号主与文件发送限额（方案B：账号类命令只允许号主）
            owner_ids=_split_set(config.get("owner_ids", "")),
            daily_file_limit=max(0, int(config.get("daily_file_limit", 0) or 0)),
            hourly_file_limit=max(0, int(config.get("hourly_file_limit", 0) or 0)),
            send_interval=max(0.0, float(config.get("send_interval", 1.5))),
            zip_group_size=min(50, max(1, configured_group or 5)),
            pdf_page_limit=min(2000, max(1, configured_pages or 100)),
            zip_page_limit=min(2000, max(0, configured_zip_pages)),
            # 翻页会话有效期：发送“下一页/下页”或 /jmnext 时使用
            page_turn_timeout=max(0, int(config.get("page_turn_timeout", 60) or 0)),
            # 后台预取页数：jmv/jmlist 当前页之后预取 N 页话页数，0=关闭
            page_prefetch_pages=min(
                5, max(0, int(config.get("page_prefetch_pages", 2) or 0))
            ),
            use_proxy=use_proxy,
            proxy_url=proxy_url,
            client_type=client_type,
            custom_sensitive_map=_parse_sensitive_map(config.get("custom_sensitive", "")),
            option=option,
        )

    def prepare_directories(self) -> None:
        """创建数据目录、清理过期缓存、初始化状态文件。"""
        self.download_base_dir.mkdir(parents=True, exist_ok=True)
        self.zip_base_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_expired_cache()
        for path in (self.history_file, self.subscription_file):
            if not path.exists():
                path.write_text("{}", encoding="utf-8")

    def _cleanup_expired_cache(self) -> None:
        """启动时自动清理过期缓存"""
        if self.cache_days <= 0:
            return
        now = time.time()
        for item in self.download_base_dir.iterdir():
            if item.is_dir() and not item.name.startswith('.'):
                mtime = item.stat().st_mtime
                if now - mtime > self.cache_days * 86400:
                    shutil.rmtree(item, ignore_errors=True)
                    logger.info(f"自动清理过期缓存: {item.name}")

    def zip_page_hint(self) -> str:
        if self.zip_page_limit <= 0:
            return "页数不限"
        return f"每册不超过 {self.zip_page_limit} 页"

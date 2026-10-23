"""jmcomic 客户端管理：登录态缓存与异常翻译。"""

import threading
import time

from astrbot.api import logger
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


class JmClientManager:
    """单实例缓存已登录客户端，避免每个命令都重新登录。"""

    # 登录态不是永久的：实测 14:58 登录后，15:28 收藏夹接口已返回 401。
    # 缓存超过 10 分钟就重新 login，仍在有效期内的命令不会重复登录。
    LOGIN_TTL_SECONDS = 600

    def __init__(self, settings):
        self.settings = settings
        self._cached_client = None
        self._cached_at = 0.0
        # 订阅检查会通过 asyncio.to_thread(get_client) 在线程里登录，
        # 命令协程可能同时调用 get_client；用线程锁覆盖“检查缓存 + 登录”。
        self._client_lock = threading.RLock()

    def _login(self, client):
        username = self.settings.username
        password = self.settings.password
        if username and password:
            try:
                client.login(username, password)
                logger.info("JMComic 客户端登录成功")
            except Exception as e:
                raise Exception(f"登录失败，请检查账号密码或镜像域名配置：{e}")
        self._cached_at = time.monotonic()

    def get_client(self):
        """创建客户端；若配置了账号密码则登录，登录态过期后自动重登。

        签到、收藏夹、搜索等接口需要登录态：
        - jmcomic 登录插件只把 cookies 写回 option，不传播 _user_id；
          新 client 的 _user_id 恒为 None，daily_checkin/get_daily 会直接报
          "签到需要传入 user_id 参数，或者先调用 login 方法"。
        - 登录失败在插件初始化阶段会被 safe=True 吞掉，导致后续请求 401。
        这里在每次创建客户端时主动 login 一次，并按 TTL 刷新。
        """
        # 优先返回仍在有效期内的已登录客户端
        with self._client_lock:
            if (
                self._cached_client is not None
                and time.monotonic() - self._cached_at < self.LOGIN_TTL_SECONDS
            ):
                return self._cached_client

            if self._cached_client is None:
                self._cached_client = self.settings.option.new_jm_client()
            self._login(self._cached_client)
            return self._cached_client

    def refresh_client(self):
        """登录态被服务端判定失效时，丢弃缓存并重新登录。"""
        with self._client_lock:
            self._cached_client = None
            self._cached_at = 0.0
            return self.get_client()

    @staticmethod
    def is_auth_error(error):
        """判断异常是否属于登录态失效（401 / 請先登入會員）。"""
        text = str(error)
        return any(
            keyword in text
            for keyword in ("401", "請先登入", "请先登入", "未登入", "未登录")
        )

    def invalidate(self):
        """客户端登录态失效时调用，下次请求会重新登录"""
        with self._client_lock:
            self._cached_client = None
            self._cached_at = 0.0

    def configured_username(self):
        try:
            for plugin in self.settings.option.plugins.after_init:
                if plugin.get('plugin') == 'login':
                    return plugin.get('kwargs', {}).get('username', '') or ''
        except Exception:
            pass
        return ''

    def handle_exception(self, e: Exception, action: str = "操作") -> str:
        """把 jmcomic 异常转换为用户友好的提示"""
        if isinstance(e, MissingAlbumPhotoException):
            return "❌ 查无此本/话，请检查 ID 是否正确"
        elif isinstance(e, RequestRetryAllFailException):
            return (
                "❌ 网络请求全部失败，可尝试在插件配置中更换域名；"
                "如已启用代理，请检查 proxy_url 是否可连通\n"
                f"{e}"
            )
        elif isinstance(e, PartialDownloadFailedException):
            return "⚠️ 部分图片下载失败，已下载图片会保留缓存，请稍后重试"
        elif DownloadCancelledException is not None and isinstance(e, DownloadCancelledException):
            return "⏹️ 下载已取消"
        else:
            logger.error(f"{action}失败: {e}", exc_info=True)
            return f"❌ {action}失败：{e}"

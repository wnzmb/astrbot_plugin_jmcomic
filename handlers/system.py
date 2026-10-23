"""域名信息、更新日志与连通性诊断。"""

import asyncio
import re
import time

from jmcomic import JmModuleConfig
from astrbot.api.event import AstrMessageEvent


class SystemHandler:
    def __init__(self, settings, access, client, messaging):
        self.settings = settings
        self.access = access
        self.client = client
        self.messaging = messaging

    def _allowed(self, event):
        return self.access.is_admin(event) or self.access.is_owner(event)

    @staticmethod
    def _normalize_domain(domain):
        domain = str(domain or "").strip()
        if not domain:
            return ""
        if not domain.startswith(("http://", "https://")):
            domain = "https://" + domain
        return domain.rstrip("/")

    def _probe_domains(self):
        """返回用于诊断的域名列表（最多 3 个，去重）。"""
        if self.settings.custom_domains:
            raw = list(self.settings.custom_domains)
        else:
            try:
                client = self.settings.option.new_jm_client()
                raw = list(client.get_domain_list())
            except Exception:
                raw = []
            if not raw:
                if self.settings.client_type == "api":
                    raw = list(
                        JmModuleConfig.DOMAIN_API_UPDATED_LIST
                        or JmModuleConfig.DOMAIN_API_LIST
                        or []
                    )
                else:
                    fallback = JmModuleConfig.DOMAIN_HTML_LIST or JmModuleConfig.DOMAIN_HTML
                    raw = list(fallback) if not isinstance(fallback, str) else [fallback]
        domains = []
        for item in raw:
            normalized = self._normalize_domain(item)
            if normalized and normalized not in domains:
                domains.append(normalized)
        return domains[:3]

    @staticmethod
    def _probe_one(domain, path, proxies, use_proxy):
        import requests

        url = domain + path
        started = time.monotonic()
        session = requests.Session()
        session.trust_env = False
        try:
            kwargs = {"timeout": 5, "allow_redirects": True}
            if use_proxy:
                kwargs["proxies"] = proxies
            resp = session.get(url, **kwargs)
            elapsed = time.monotonic() - started
            ok = 200 <= resp.status_code < 400
            return domain, ok, elapsed, f"HTTP {resp.status_code}"
        except Exception as e:
            return domain, False, time.monotonic() - started, str(e)[:100]
        finally:
            session.close()

    @staticmethod
    def _mask_proxy(url):
        text = str(url or "")
        if "://" not in text or "@" not in text:
            return text
        scheme, rest = text.split("://", 1)
        userinfo, host = rest.rsplit("@", 1)
        user = userinfo.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"

    async def test(self, event: AstrMessageEvent):
        """诊断配置域名、代理和登录态，用法：/jmtest"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        if not self._allowed(event):
            yield self.messaging.plain(event, "⚠️ 只有管理员或号主可以使用 /jmtest")
            return

        yield self.messaging.plain(event, "🔧 正在诊断 JM 连通性...")
        client_label = "API" if self.settings.client_type == "api" else "HTML"
        proxied = bool(self.settings.use_proxy and self.settings.proxy_url)
        proxies = (
            {"http": self.settings.proxy_url, "https": self.settings.proxy_url}
            if proxied else None
        )
        lines = [
            "🔧 JM 连通性诊断",
            f"客户端：{client_label}",
            f"代理：{'已启用 ' + self._mask_proxy(self.settings.proxy_url) if proxied else '未启用（直连）'}",
        ]

        domains = self._probe_domains()
        if domains:
            path = "/setting" if self.settings.client_type == "api" else "/"
            results = await asyncio.gather(
                *(
                    asyncio.to_thread(
                        self._probe_one, domain, path, proxies, proxied
                    )
                    for domain in domains
                )
            )
            lines.append("\n🌐 域名探测：")
            for domain, ok, elapsed, detail in results:
                mark = "✅" if ok else "❌"
                lines.append(f"  {mark} {domain}  {elapsed:.2f}s  {detail}")
        else:
            lines.append("\n🌐 域名探测：未获取到可用域名")

        if self.settings.username and self.settings.password:
            try:
                client = await asyncio.wait_for(
                    asyncio.to_thread(self.client.get_client), timeout=15
                )
                uid = getattr(client, "_user_id", None)
                if uid:
                    await asyncio.wait_for(
                        asyncio.to_thread(client.get_daily), timeout=10
                    )
                    lines.append("\n🔐 登录态：✅ 正常")
                else:
                    lines.append("\n🔐 登录态：⚠️ 已创建客户端但未获取到 uid")
            except asyncio.TimeoutError:
                lines.append("\n🔐 登录态：❌ 超时（15 秒）")
            except Exception as e:
                lines.append(f"\n🔐 登录态：❌ {self.client.handle_exception(e, '登录检查')}")
        else:
            lines.append("\n🔐 登录态：未配置账号密码（跳过）")

        lines.append("\n💡 域名或代理失败时，可在配置页调整后重试。")
        yield self.messaging.plain(event, "\n".join(lines))

    async def domain(self, event: AstrMessageEvent):
        """查看 API 与 HTML 两套可用域名"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        yield self.messaging.plain(event, "🌐 正在获取可用域名...")
        try:
            client = self.client.get_client()
            client_label = "API" if self.settings.client_type == "api" else "HTML"

            api_domains = []
            html_domains = []

            # 当前客户端已知域名
            try:
                current_domains = list(client.get_domain_list())
                if self.settings.client_type == "api":
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

            custom_domains = list(self.settings.custom_domains)

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

    async def changelog(self, event: AstrMessageEvent):
        """查看插件更新日志"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        changelog_path = self.settings.plugin_dir / "CHANGELOG.md"
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

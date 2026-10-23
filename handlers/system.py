"""域名信息与更新日志。"""

import asyncio
import re

from jmcomic import JmModuleConfig
from astrbot.api.event import AstrMessageEvent


class SystemHandler:
    def __init__(self, settings, access, client, messaging):
        self.settings = settings
        self.access = access
        self.client = client
        self.messaging = messaging

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

"""翻页导航：/jmnext 与“下一页/下页”关键词。"""

from astrbot.api.event import AstrMessageEvent

from ..services.pagination import PageEventProxy


class NavigationHandler:
    def __init__(self, access, pagination, messaging, dispatch):
        self.access = access
        self.pagination = pagination
        self.messaging = messaging
        self.dispatch = dispatch

    async def follow(self, event, session):
        next_command = session.get("next_command", "")
        parts = next_command.strip().split()
        if not parts:
            return
        command = parts[0].lstrip("/").lower()
        handler_name = self.pagination.page_handler_name(command)
        handler = self.dispatch.get(handler_name) if handler_name else None
        if handler is None:
            return
        proxy = PageEventProxy(event, next_command)
        async for message in handler(proxy):
            yield message

    async def next(self, event: AstrMessageEvent):
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
        if session.get("at_last_page"):
            self.pagination.clear(event)
            yield self.messaging.plain(event, "已经是最后一页了")
            return
        self.pagination.clear(event)
        async for message in self.follow(event, session):
            yield message

    async def next_keyword(self, event: AstrMessageEvent):
        """在有效期内发送“下一页/下页”自动翻页；无会话时静默。"""
        if not self.access.check_permission(event):
            return
        session = self.pagination.get(event)
        if session is None:
            return
        if session.get("at_last_page"):
            self.pagination.clear(event)
            yield self.messaging.plain(event, "已经是最后一页了")
            return
        self.pagination.clear(event)
        async for message in self.follow(event, session):
            yield message

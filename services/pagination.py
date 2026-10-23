"""翻页会话：按 (umo, sender_id) 隔离，带超时与 PageEventProxy。"""

import time


class PageEventProxy:
    """给 /jmnext 复用一个新命令字符串，其余事件能力仍委托原事件。"""

    def __init__(self, event, message_str):
        object.__setattr__(self, "_event", event)
        object.__setattr__(self, "message_str", message_str)

    def get_message_str(self):
        return self.message_str

    def __getattr__(self, name):
        return getattr(self._event, name)


class PageSessionService:
    def __init__(self, settings, state):
        self.settings = settings
        self.state = state

    @staticmethod
    def page_session_key(event):
        umo = getattr(event, "unified_msg_origin", "") or ""
        return (str(umo), str(event.get_sender_id()))

    def remember(self, event, next_command, page, total_pages=None):
        if self.settings.page_turn_timeout <= 0 or not next_command:
            return
        key = self.page_session_key(event)
        if total_pages is not None and page >= int(total_pages):
            # 不直接删会话，否则用户再发 /jmnext 只能看到“没有列表/已超时”，
            # 无法区分“已经是最后一页”。保留一个仅用于提示的会话。
            self.state.page_sessions[key] = {
                "next_command": "",
                "page": int(page),
                "total_pages": int(total_pages),
                "expires_at": time.monotonic() + self.settings.page_turn_timeout,
                "at_last_page": True,
            }
            return
        self.state.page_sessions[key] = {
            "next_command": next_command,
            "page": int(page),
            "total_pages": int(total_pages) if total_pages is not None else None,
            "expires_at": time.monotonic() + self.settings.page_turn_timeout,
        }

    def get(self, event):
        key = self.page_session_key(event)
        session = self.state.page_sessions.get(key)
        if not session:
            return None
        if time.monotonic() > session.get("expires_at", 0):
            self.state.page_sessions.pop(key, None)
            return None
        return session

    def clear(self, event):
        self.state.page_sessions.pop(self.page_session_key(event), None)

    @staticmethod
    def page_handler_name(command):
        return {
            "jmlist": "jm_chapter_list",
            "jmhis": "jm_history",
            "jmc": "jm_comments",
            "jmrank": "jm_ranking",
            "jms": "jm_search",
            "jma": "jm_search_author",
            "jmt": "jm_search_tag",
            "jmr": "jm_search_actor",
            "jmw": "jm_search_work",
        }.get(command)

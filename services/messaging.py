"""统一消息输出：敏感词/繁简转换、QQ 官方消息预算、日志违规检测。

这是唯一直接调用 event.plain_result / event.chain_result 的层。
"""

from pathlib import Path

import astrbot.api.message_components as Comp


# 账号类命令不经过全局敏感词转换，避免把用户 ID/账号信息误处理
ACCOUNT_COMMANDS = {"jmcheckin", "jmme", "jmfav", "jmwhoami"}


class MessagingService:
    def __init__(self, settings, text):
        self.settings = settings
        self.text = text

    def command_name(self, event):
        try:
            first = (getattr(event, "message_str", "") or "").strip().split()[0]
            return first[1:] if first.startswith("/") else first
        except Exception:
            return ""

    def output_text(self, event, text):
        """所有非账号类输出统一做繁简/敏感词处理"""
        if self.command_name(event) in ACCOUNT_COMMANDS:
            return text
        return self.text.safe_text(text)

    def plain(self, event, text):
        return event.plain_result(self.output_text(event, text))

    def chain(self, event, chain):
        """处理 chain 中的文本组件，文件/图片组件保持原样"""
        if self.command_name(event) in ACCOUNT_COMMANDS:
            return event.chain_result(chain)
        safe_chain = []
        for component in chain:
            if isinstance(component, Comp.Plain):
                safe_chain.append(Comp.Plain(self.output_text(event, getattr(component, "text", ""))))
            else:
                safe_chain.append(component)
        return event.chain_result(safe_chain)

    @staticmethod
    def is_qq_official(event):
        try:
            return event.get_platform_name() == "qq_official"
        except Exception:
            return False

    def message_budget(self, event):
        """QQ 官方单聊 4 条、群聊 5 条；其他平台不限制。"""
        if not self.is_qq_official(event):
            return None
        try:
            return 5 if str(event.get_group_id() or "") else 4
        except Exception:
            return 4

    # ========== QQ 消息违规检测 ==========
    def backend_log_path(self):
        """定位 AstrBot 的 backend.log，用于检测平台静默拦截"""
        candidates = []
        try:
            # .../.astrbot/data/plugin_data/<plugin> -> .../.astrbot/logs/backend.log
            candidates.append(self.settings.data_dir.parents[2] / "logs" / "backend.log")
            candidates.append(self.settings.data_dir.parents[1] / "logs" / "backend.log")
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

    def log_pos(self):
        path = self.backend_log_path()
        if path is None:
            return 0
        try:
            return path.stat().st_size
        except Exception:
            return 0

    def log_has_violation(self, offset):
        """从 offset 开始读日志，检测“消息内容违规”类错误"""
        path = self.backend_log_path()
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
    def is_violation_error(err):
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

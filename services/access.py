"""访问控制：群聊总开关、群白名单、私聊例外、号主判断。"""


class AccessService:
    def __init__(self, settings):
        self.settings = settings

    def check_permission(self, event) -> bool:
        """检查群聊权限：allow_group 总开关 + enabled_groups 白名单"""
        group_id = event.get_group_id()
        if not group_id:
            # 私聊不受群开关和白名单影响
            return True
        if not self.settings.allow_group:
            return False
        if self.settings.enabled_groups and str(group_id) not in self.settings.enabled_groups:
            return False
        return True

    def permission_denied_text(self, event) -> str:
        group_id = event.get_group_id()
        if group_id and not self.settings.allow_group:
            return "本插件已关闭群聊使用，请私聊使用"
        return (
            "本群不在插件群白名单中，请私聊使用，"
            "或让管理员把本群号加入 enabled_groups"
        )

    def is_admin(self, event) -> bool:
        """AstrBot 管理员判断：优先用事件自带方法，旧版本回退到 role 字段。"""
        if hasattr(event, "is_admin"):
            return bool(event.is_admin())
        return getattr(event, "role", "") == "admin"

    def is_owner(self, event) -> bool:
        """方案B：只有配置在 owner_ids 里的发送者才能使用账号类命令"""
        if not self.settings.owner_ids:
            return False
        return str(event.get_sender_id()) in self.settings.owner_ids

    def owner_required_text(self) -> str:
        if not self.settings.owner_ids:
            return (
                "⚠️ 账号类命令仅限 JM 账号号主使用。\n"
                "请先在插件配置里填写 owner_ids（号主ID），"
                "发送 /jmwhoami 可查看你的ID。"
            )
        return "⚠️ 账号类命令仅限 JM 账号号主使用。"

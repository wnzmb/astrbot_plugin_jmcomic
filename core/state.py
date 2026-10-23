"""运行期共享状态容器。只放跨命令共享的字段，不放单次命令的局部变量。"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PluginState:
    # 目标 ID 类型缓存：key -> "album"/"photo"
    id_type_cache: dict = field(default_factory=dict)
    # 当前用户正在执行的下载任务，供 /jmstop 取消
    download_tasks: dict[str, set] = field(default_factory=dict)
    # 当前用户下载进度，供 /jmnow 查看
    active_downloads: dict[str, dict] = field(default_factory=dict)
    # 最近一次分页结果，供 /jmnext 和“下一页/下页”使用
    page_sessions: dict[tuple, dict] = field(default_factory=dict)
    # 收藏夹导出二次确认：key=(发送者, umo, 操作) -> 首次输入时间
    pending_exports: dict[tuple, float] = field(default_factory=dict)
    # 评论总页数缓存：key -> 页数
    comment_page_cache: dict = field(default_factory=dict)
    # 订阅更新后台检查任务
    subscribe_task: Any = None
    subscription_lock: Any = None

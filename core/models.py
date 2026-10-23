"""轻量数据模型。"""

from typing import Any, NamedTuple


class PageUnit(NamedTuple):
    """一个待打包的页段：章节内第 start~end 页（1 起，含两端）"""

    index: int
    photo: Any
    start: int
    end: int
    total: int

    @property
    def pages(self):
        return max(0, self.end - self.start + 1)

    @property
    def partial(self):
        return self.start > 1 or self.end < self.total

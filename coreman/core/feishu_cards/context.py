"""一次编译的上下文：element_id 分配、图片 key 映射、各类组件的用量计数。

各渲染器只管把自己那一块转成组件；数量上限（表格、图表）在这里记账，超了由渲染器降级。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

# 飞书上限：table 组件每卡 5 个；chart 建议每卡 5 个。
MAX_TABLES = 5
MAX_CHARTS = 5
_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_]")


@dataclass
class RenderContext:
    """images：远程图片 URL → 上传后的 img_key；没有映射的图片渲染成链接。

    people：邮箱 / 登录名 → 飞书 user_id；没有映射的人按名字文字显示。
    """

    images: Mapping[str, str] = field(default_factory=dict)
    people: Mapping[str, str] = field(default_factory=dict)
    # 回复按钮要网关接住回调才能用；没接上之前渲染时直接略过，免得点了报「操作失败」。
    allow_reply: bool = False
    tables: int = 0
    charts: int = 0
    _seq: dict[str, int] = field(default_factory=dict)

    def new_id(self, prefix: str) -> str:
        """element_id：字母开头、只含字母数字下划线、≤20 字符、全卡唯一。"""
        head = _ID_UNSAFE.sub("", prefix)[:8] or "e"
        if not head[0].isalpha():
            head = "e" + head[:7]
        n = self._seq.get(head, 0) + 1
        self._seq[head] = n
        return f"{head}_{n}"

    def image_key(self, url: str) -> str | None:
        return self.images.get(url)

    def take_table(self) -> bool:
        """占用一个 table 组件名额；用完返回 False（调用方改用 markdown 表格）。"""
        if self.tables >= MAX_TABLES:
            return False
        self.tables += 1
        return True

    def take_chart(self) -> bool:
        """占用一个 chart 名额；用完返回 False（调用方降级成数据表）。"""
        if self.charts >= MAX_CHARTS:
            return False
        self.charts += 1
        return True

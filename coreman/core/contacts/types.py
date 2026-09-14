"""平台无关的通讯录目录结构（企微、飞书抓取后都转成这个）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DirectoryDept:
    platform_dept_id: str
    parent_platform_dept_id: str | None
    name: str
    sort_order: int = 0


@dataclass
class DirectoryUser:
    platform_user_id: str
    name: str
    dept_ids: list[str]
    main_dept_id: str | None
    position: str | None
    mobile: str | None
    email: str | None
    avatar_url: str | None
    active: bool
    profile: dict[str, Any] = field(default_factory=dict)


@dataclass
class Directory:
    platform: str
    departments: list[DirectoryDept]
    users: list[DirectoryUser]

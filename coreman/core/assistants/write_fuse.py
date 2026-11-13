"""写操作保险丝：助理记录用所有者的飞书令牌调非读接口，只在平台执行已批准的单时放行。

拦截（暂存参数、提交审批）在 MCP 入口做；这里是它身后的最后一道：所有带个人令牌的请求都经过
`feishu_personal.service._authorize`，那里对助理记录调用 `blocked`。以后新增入口、新增工具、
某个读类工具内部多调了一个写接口，都过不去。普通机器人只多一次属性比较。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from coreman.core.db.models import Bot

_ALLOWED: ContextVar[uuid.UUID | None] = ContextVar("assistant_write_allowed", default=None)


@contextmanager
def allow(bot_id: uuid.UUID) -> Iterator[None]:
    """只给两处用：执行已批准的审批单、平台限定的「处理中」表情。"""
    token = _ALLOWED.set(bot_id)
    try:
        yield
    finally:
        _ALLOWED.reset(token)


def blocked(bot: Bot, kind: str) -> bool:
    return kind != "read" and bot.kind == "assistant" and _ALLOWED.get() != bot.id

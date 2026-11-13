"""业务系统目录的代理调用：个人助理调度来的只读一轮不放写。"""

from __future__ import annotations

import uuid

from coreman.core.assistants.kinds import BOT_HELP_KIND
from coreman.core.db.models import Task
from coreman.core.systems_catalog import policy


def _task(kind: str, **payload: object) -> Task:
    return Task(bot_id=uuid.uuid4(), kind=kind, payload=payload)


def test_a_read_only_dispatch_cannot_write_through_the_catalog() -> None:
    granted = ["stock", "orders"]
    assert policy.writable_for(_task("chat"), granted) == frozenset(granted)
    assert policy.writable_for(_task(BOT_HELP_KIND), granted) == frozenset()
    assert policy.writable_for(_task(BOT_HELP_KIND, allow_write=False), granted) == frozenset()
    assert policy.writable_for(_task(BOT_HELP_KIND, allow_write=True), granted) == frozenset(
        granted
    )
    # 只认布尔真：载荷里写成字符串不算批准过。
    assert policy.writable_for(_task(BOT_HELP_KIND, allow_write="true"), granted) == frozenset()

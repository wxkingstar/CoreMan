"""普通机器人的路径不因加密存储多查库：结束任务、结掉记录、修订回复各只有一条写语句。

这三个入口封存内容前要知道这一行属于谁。调用方都把手里的 bot_id 传进来；没传时才按主键
查一次（回退）。归属本身在进程内缓存，这里先预热，数的是稳定状态下的语句。
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import sealed
from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.chat import chat_logs
from coreman.core.chat.chat_logs import ChatLogEntry, ChatLogWriter
from coreman.core.db.models import Bot
from coreman.core.db.session import make_session_factory
from tests.integration.assistant_helpers import raw, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def _statements(engine: AsyncEngine) -> Iterator[list[str]]:
    seen: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        if not statement.startswith(("SAVEPOINT", "RELEASE", "BEGIN", "COMMIT", "ROLLBACK")):
            seen.append(" ".join(statement.split()))

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


async def _running_turn(session: AsyncSession, bot: Bot) -> int:
    task = await tasks.enqueue(
        session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key="oc_1")
    )
    assert task is not None
    assert await chat_logs.open_turn(
        session,
        ChatLogEntry(
            bot_id=bot.id,
            bot_key=bot.bot_key,
            platform="feishu",
            chat_type="single",
            message_type="text",
            status="running",
            request_at=datetime.now(UTC),
            task_id=task.id,
            message_content="你好",
        ),
    )
    await session.commit()
    return task.id


async def test_standard_bot_finishing_and_amending_adds_no_lookup(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot = await seed_standard(db_session)
    first = await _running_turn(db_session, bot)
    second = await _running_turn(db_session, bot)
    await sealed.owner_of(db_session, bot.id)
    with _statements(db_engine) as seen:
        assert await tasks.finish(
            db_session,
            first,
            status="failed",
            result={"ok": False},
            error_code="handler_crashed",
            error_message="出错了",
            bot_id=bot.id,
        )
        assert await chat_logs.close_running(
            db_session,
            first,
            status="error",
            error_code="handler_crashed",
            error_message="出错了",
            bot_id=bot.id,
        )
        await db_session.commit()
    assert [s.split(" ", 2)[:2] for s in seen] == [["UPDATE", "tasks"], ["UPDATE", "chat_logs"]]
    assert await tasks.finish(db_session, second, status="succeeded", bot_id=bot.id)
    await chat_logs.close_running(
        db_session, second, status="success", error_code=None, error_message=None
    )
    await db_session.commit()
    writer = ChatLogWriter(make_session_factory(db_engine))
    with _statements(db_engine) as seen:
        assert await writer.amend_response(second, "终稿 + 额度表", bot_id=bot.id)
    assert [s.split(" ", 2)[:2] for s in seen] == [["UPDATE", "chat_logs"]]
    assert (
        await raw(
            db_session, "SELECT response_content FROM chat_logs WHERE task_id = :id", id=second
        )
        == "终稿 + 额度表"
    )


async def test_the_lookup_remains_as_a_fallback(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot = await seed_standard(db_session)
    task_id = await _running_turn(db_session, bot)
    await sealed.owner_of(db_session, bot.id)
    with _statements(db_engine) as seen:
        assert await tasks.finish(db_session, task_id, status="succeeded", result={"ok": True})
    assert [s.split(" ", 2)[:2] for s in seen] == [["SELECT", "tasks.bot_id"], ["UPDATE", "tasks"]]
    with _statements(db_engine) as seen:
        # 没有结果也没有报错信息要写：不传 bot_id 也不查。
        await tasks.finish(db_session, task_id, status="succeeded")
    assert [s.split(" ", 2)[:2] for s in seen] == [["UPDATE", "tasks"]]


def _called(node: ast.Call) -> tuple[str, str]:
    func = node.func
    if isinstance(func, ast.Attribute):
        owner = func.value.id if isinstance(func.value, ast.Name) else ""
        return owner, func.attr
    return "", func.id if isinstance(func, ast.Name) else ""


def test_every_caller_hands_over_the_bot_it_already_holds() -> None:
    """带结果或报错信息的结束、结掉记录、修订回复：调用点都要传 bot_id，回退查询只是兜底。"""
    missing: list[str] = []
    for path in sorted((ROOT / "coreman").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            owner, name = _called(node)
            keywords = {keyword.arg for keyword in node.keywords}
            carries_content = (
                (name == "finish" and owner == "tasks" and keywords & {"result", "error_message"})
                or (name == "finish" and owner == "" and relative == "coreman/core/bus/tasks.py")
                or (name == "close_running" and owner == "chat_logs")
                or name == "submit_amend"
            )
            if carries_content and "bot_id" not in keywords:
                missing.append(f"{relative}:{node.lineno}")
    assert missing == []

"""个人助理的内容不进日志：报错原文只留代码与长度，异常只留类型与调用栈。"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from structlog.testing import capture_logs

from coreman.core import logging as core_logging
from coreman.core.assistants import sealed
from coreman.core.bus import outbox, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import Bot
from coreman.core.db.session import make_session_factory
from coreman.core.logging import configure_logging, get_logger
from tests.integration.assistant_helpers import SECRET, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
ERROR = f"errcode=40058 invalid content: {SECRET}"


async def _fail_with_dependents(session: AsyncSession, bot: Bot) -> list[dict[str, Any]]:
    """同一组里前一条永久失败、后一条还在排队：会留一条「后续项不再等它」的告警。"""
    ids = []
    for part in ("0", "1"):
        item = await outbox.add(
            session,
            bot_id=bot.id,
            platform="feishu",
            kind="send",
            dedupe_key=f"7:part:{part}",
            target={"chat_id": "oc_1"},
            payload={"markdown": "hi"},
        )
        assert item is not None
        ids.append(item.id)
    await session.commit()
    with capture_logs() as logs:
        await outbox.fail(session, ids[0], ERROR)
        await session.commit()
    return [entry for entry in logs if entry["event"] == "outbox_dependents_released"]


async def test_assistant_failure_warning_has_no_error_text(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    (entry,) = await _fail_with_dependents(db_session, bot)
    assert SECRET not in json.dumps(entry, ensure_ascii=False, default=str)
    assert entry["error"] == "redacted" and entry["error_length"] == len(ERROR)
    assert entry["released"] == 1


async def test_assistant_failure_warning_keeps_a_leading_code(db_session: AsyncSession) -> None:
    bot, _ = await seed_assistant(db_session)
    item = await outbox.add(
        db_session,
        bot_id=bot.id,
        platform="feishu",
        kind="send",
        dedupe_key="9:part:0",
        target={"chat_id": "oc_1"},
        payload={"markdown": "hi"},
    )
    later = await outbox.add(
        db_session,
        bot_id=bot.id,
        platform="feishu",
        kind="send",
        dedupe_key="9:part:1",
        target={"chat_id": "oc_1"},
        payload={"markdown": "hi"},
    )
    assert item is not None and later is not None
    await db_session.commit()
    with capture_logs() as logs:
        await outbox.fail(db_session, item.id, f"Feishu 230013: {SECRET}")
        await db_session.commit()
    (entry,) = [e for e in logs if e["event"] == "outbox_dependents_released"]
    assert entry["error"] == "Feishu 230013" and SECRET not in str(entry)


async def test_standard_bot_failure_warning_is_unchanged(db_session: AsyncSession) -> None:
    bot = await seed_standard(db_session)
    (entry,) = await _fail_with_dependents(db_session, bot)
    assert entry["error"] == ERROR and "error_length" not in entry


def _crash(marked: bool) -> tuple[dict[str, Any], str]:
    """处理中抛出带内容的异常并记日志；返回 JSON 行与进程写出的全部日志文本。"""
    out = io.StringIO()
    configure_logging(service="worker", instance="w", stream=out)
    try:
        core_logging.mark_private_content(marked)
        try:
            raise ValueError(f"bad value: {SECRET}")
        except ValueError:
            get_logger("test").exception("task_handler_crashed", kind="chat")
    finally:
        core_logging.mark_private_content(False)
        structlog.reset_defaults()
    text = out.getvalue()
    return dict(json.loads(text.splitlines()[0])), text


def test_exception_text_is_dropped_while_handling_assistant_content() -> None:
    record, text = _crash(marked=True)
    assert SECRET not in text
    # 类型与调用栈还在，够定位。
    assert record["exception"].startswith("ValueError")
    assert "test_assistant_log_redaction.py" in record["exception"]


def test_exception_text_is_kept_otherwise() -> None:
    record, text = _crash(marked=False)
    assert f"ValueError: bad value: {SECRET}" in record["exception"]
    # 标准库附在后面的那份 traceback 也照旧。
    assert text.rstrip().endswith(f"ValueError: bad value: {SECRET}")


async def test_only_assistant_rows_switch_the_guard_on(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """worker 开始一个任务、飞书网关开始一轮时调用：之后这段处理里的异常文字不进日志。"""
    assistant, _ = await seed_assistant(db_session)
    standard = await seed_standard(db_session)
    factory = make_session_factory(db_engine)
    seen = {}
    for name, bot in (("assistant", assistant), ("standard", standard)):
        task = await tasks.enqueue(db_session, NewTask(bot_id=bot.id, kind="chat", payload={}))
        await db_session.commit()
        assert task is not None
        async with factory() as session:
            await sealed.guard_logs(session, task.bot_id)
        seen[name] = core_logging.private_content()
    core_logging.mark_private_content(False)
    assert seen == {"assistant": True, "standard": False}

"""访问记录：只有编号、动作与对象代号；与主操作同一个事务；写不进去不挡主操作；按人查、按期清。"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from coreman.core.assistants import access_log
from coreman.core.db.models import AssistantAccessLog, User
from coreman.core.timeutils import utcnow
from tests.conftest import BUSINESS_TABLES
from tests.integration.assistant_helpers import seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")
STORED = (
    "SELECT actor_id, actor_kind, action, target FROM assistant_access_log"
    " WHERE bot_id = :bot ORDER BY id"
)


async def _stored(session: AsyncSession, bot_id: uuid.UUID) -> list[tuple[Any, ...]]:
    rows = await session.execute(text(STORED), {"bot": bot_id})
    return [tuple(row) for row in rows.all()]


def test_table_is_truncated_with_the_other_business_tables() -> None:
    assert "assistant_access_log" in BUSINESS_TABLES


async def test_the_table_has_no_column_that_could_hold_content(db_session: AsyncSession) -> None:
    rows = await db_session.execute(
        text(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'assistant_access_log'"
        )
    )
    assert {row[0] for row in rows.all()} == {
        "id",
        "bot_id",
        "actor_id",
        "actor_kind",
        "action",
        "target",
        "at",
    }


async def test_a_read_by_the_owner_is_stored_and_logged(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    with capture_logs() as logs:
        await access_log.record(db_session, bot.id, owner.id, "followups.read", "open")
    await db_session.commit()
    assert await _stored(db_session, bot.id) == [(owner.id, "owner", "followups.read", "open")]
    assert logs == [
        {
            "event": "assistant_content_access",
            "log_level": "info",
            "bot_id": str(bot.id),
            "actor_id": str(owner.id),
            "actor_kind": "owner",
            "action": "followups.read",
            "target": "open",
        }
    ]


async def test_the_actor_kind_follows_who_acted(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    other = User(login_name="auditor", display_name="李四")
    db_session.add(other)
    await db_session.flush()
    await access_log.record(db_session, bot.id, other.id, "chatlogs.read", "rows=1")
    await access_log.record(db_session, bot.id, None, "cli.rewrap")
    await access_log.record(db_session, bot.id, owner.id, "cli.chatlogs", "rows=1", kind="system")
    await db_session.commit()
    assert await _stored(db_session, bot.id) == [
        (other.id, "admin", "chatlogs.read", "rows=1"),
        (None, "system", "cli.rewrap", ""),
        (owner.id, "system", "cli.chatlogs", "rows=1"),
    ]
    with pytest.raises(ValueError, match="unknown actor kind"):
        await access_log.record(db_session, bot.id, owner.id, "notes.read", "page", kind="robot")


async def test_the_target_is_cut_and_unknown_actions_are_rejected(
    db_session: AsyncSession,
) -> None:
    bot, owner = await seed_assistant(db_session)
    # 回滚会让会话里的对象过期：之后还要用的编号先取出来。
    bot_id, owner_id = bot.id, owner.id
    await access_log.record(db_session, bot_id, owner_id, "jobs.read", "x" * 500)
    await db_session.commit()
    [(_, _, _, target)] = await _stored(db_session, bot_id)
    assert target == "x" * access_log.MAX_TARGET
    with pytest.raises(ValueError, match="unknown access action"):
        await access_log.record(db_session, bot_id, owner_id, "pages.dump", "")
    await db_session.rollback()
    assert len(await _stored(db_session, bot_id)) == 1


async def test_the_row_shares_the_callers_transaction(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    bot_id = bot.id
    await access_log.record(db_session, bot_id, owner.id, "notes.read", "page")
    await db_session.rollback()
    assert await _stored(db_session, bot_id) == []


async def test_a_failed_insert_does_not_block_the_main_operation(
    db_session: AsyncSession,
) -> None:
    _bot, owner = await seed_assistant(db_session)
    # 主操作攒着的改动。
    db_session.add(User(login_name="probe", display_name="李四"))
    nowhere = uuid.uuid4()
    with capture_logs() as logs:
        # 没有这条助理记录：外键对不上，这一行写不进去。
        await access_log.record(db_session, nowhere, owner.id, "notes.read", "page")
    await db_session.commit()
    assert [entry["event"] for entry in logs] == [
        "assistant_content_access",
        "assistant_access_log_failed",
    ]
    assert logs[1]["error"] == "IntegrityError" and logs[1]["action"] == "notes.read"
    # 主操作照常提交了；那一行没有留下。
    assert await db_session.scalar(select(User.id).where(User.login_name == "probe")) is not None
    assert await _stored(db_session, nowhere) == []


async def test_a_deleted_actor_leaves_the_row_without_a_name(db_session: AsyncSession) -> None:
    bot, _owner = await seed_assistant(db_session)
    other = User(login_name="leaver", display_name="李四")
    db_session.add(other)
    await db_session.flush()
    await access_log.record(db_session, bot.id, other.id, "chatlogs.read", "rows=1")
    await db_session.commit()
    await db_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": other.id})
    await db_session.commit()
    assert await _stored(db_session, bot.id) == [(None, "admin", "chatlogs.read", "rows=1")]


async def test_listing_is_newest_first_pages_by_id_and_names_the_actor(
    db_session: AsyncSession,
) -> None:
    bot, owner = await seed_assistant(db_session)
    other = User(login_name="auditor", display_name="李四")
    db_session.add(other)
    await db_session.flush()
    await access_log.record(db_session, bot.id, owner.id, "notes.index", "")
    await access_log.record(db_session, bot.id, other.id, "chatlogs.read", "rows=2")
    await access_log.record(db_session, bot.id, None, "cli.rewrap", "")
    await db_session.commit()
    rows = await access_log.list_(db_session, bot.id, limit=2)
    assert [(row.action, row.actor_kind, row.actor_name) for row in rows] == [
        ("cli.rewrap", "system", None),
        ("chatlogs.read", "admin", "李四"),
    ]
    older = await access_log.list_(db_session, bot.id, limit=2, before=rows[-1].id)
    assert [(row.action, row.actor_kind, row.actor_name, row.target) for row in older] == [
        ("notes.index", "owner", "张三", "")
    ]
    # 别人的助理：一行都拿不到。
    theirs, _their_owner = await seed_assistant(db_session)
    assert await access_log.list_(db_session, theirs.id) == []
    # 「最近一次非所有者访问」不数所有者自己的。
    assert await access_log.last_foreign(db_session) == {bot.id: rows[0].at}


async def test_rows_older_than_180_days_are_removed_in_batches(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    now = utcnow()
    for days in (181, 181, 181, 179, 0):
        db_session.add(
            AssistantAccessLog(
                bot_id=bot.id,
                actor_id=owner.id,
                actor_kind="owner",
                action="notes.read",
                target="page",
                at=now - timedelta(days=days),
            )
        )
    await db_session.commit()
    assert await access_log.cleanup(db_session, now, limit=2) == 2
    assert await access_log.cleanup(db_session, now, limit=2) == 1
    assert await access_log.cleanup(db_session, now, limit=2) == 0
    await db_session.commit()
    left = await db_session.scalar(select(func.count()).select_from(AssistantAccessLog))
    assert left == 2

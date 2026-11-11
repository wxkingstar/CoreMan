"""interaction_states：提问卡的题面、选项、答案与暂存参数加密入库；点击与续接读回明文。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.chat import interactions
from coreman.core.db.models import Bot, InteractionState
from coreman.core.db.session import make_session_factory
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
STATE = "SELECT state::text FROM interaction_states WHERE id = :id"
QUESTIONS = [{"question": f"选哪个？{SECRET}", "options": [{"label": f"甲-{SECRET}"}]}]


def _choice(bot: Bot) -> dict[str, Any]:
    return {
        "questions": QUESTIONS,
        "answers": [],
        "current_index": 0,
        "context": {"platform_user_id": "ou_1", "chat_id": "oc_1", "message": SECRET},
        "user_id": "ou_1",
    }


async def _open(session: AsyncSession, bot: Bot) -> InteractionState:
    row = await interactions.open_state(
        session,
        bot_id=bot.id,
        kind=interactions.KIND_CHOICE,
        scope_key=interactions.choice_scope(bot.id, "ou_1"),
        state=_choice(bot),
        task_id_prefix="ask1",
    )
    await session.commit()
    return row


async def test_choice_state_is_sealed_and_reads_back(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    row = await _open(db_session, bot)
    # 建完交回的行就是明文。
    assert row.state["questions"] == QUESTIONS
    stored = str(await raw(db_session, STATE, id=row.id))
    assert SECRET not in stored and "enc:v2:d1:" in stored
    # 按人数待确认条数的 SQL 过滤用的键留在密文外面。
    assert json.loads(stored)["user_id"] == "ou_1"
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(InteractionState)
            .where(InteractionState.state["user_id"].astext == "ou_1")
        )
        == 1
    )
    factory = make_session_factory(db_engine)
    async with factory() as other:
        # 卡片点击：按前缀找回这一轮，记下答案。
        found = await interactions.find_by_prefix(other, bot_id=bot.id, task_id_prefix="ask1")
        assert found is not None and found.state["questions"] == QUESTIONS
        await interactions.patch_state(
            other, found.id, {"answers": [f"答-{SECRET}"], "current_index": 1}, bot_id=bot.id
        )
        await other.commit()
    stored = str(await raw(db_session, STATE, id=row.id))
    assert SECRET not in stored and "current_index" not in stored
    async with factory() as other:
        # 续接：下一条消息来时取回还开着的那条。不传 bot_id 的合并按主键查归属。
        again = await interactions.get_open(
            other,
            kind=interactions.KIND_CHOICE,
            scope_key=interactions.choice_scope(bot.id, "ou_1"),
        )
        assert again is not None
        assert again.state == {**_choice(bot), "answers": [f"答-{SECRET}"], "current_index": 1}
        await interactions.patch_state(other, again.id, {"waiting_for_text": True})
        await other.commit()
        fetched = await other.get(InteractionState, row.id, populate_existing=True)
        assert fetched is not None and fetched.state["waiting_for_text"] is True
        assert fetched.state["questions"] == QUESTIONS
    assert "waiting_for_text" not in str(await raw(db_session, STATE, id=row.id))


async def test_destroyed_key_makes_the_state_look_gone(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    row = await _open(db_session, bot)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    scope = interactions.choice_scope(bot.id, "ou_1")
    assert await interactions.get_open(db_session, kind="choice", scope_key=scope) is None
    assert (
        await interactions.find_by_prefix(db_session, bot_id=bot.id, task_id_prefix="ask1") is None
    )
    # 销毁之后的合并不抛错，内容丢弃。
    await interactions.patch_state(db_session, row.id, {"answers": [SECRET]}, bot_id=bot.id)
    await db_session.commit()
    stored = json.loads(await raw(db_session, STATE, id=row.id))
    assert stored == {"_sealed": "", "user_id": "ou_1"}
    fetched = await db_session.get(InteractionState, row.id, populate_existing=True)
    assert sealed.is_locked(fetched)


async def test_standard_bot_state_stays_plain(db_session: AsyncSession) -> None:
    bot = await seed_standard(db_session)
    row = await _open(db_session, bot)
    await interactions.patch_state(
        db_session, row.id, {"answers": [SECRET], "current_index": 1}, bot_id=bot.id
    )
    await db_session.commit()
    stored = json.loads(await raw(db_session, STATE, id=row.id))
    assert stored == {**_choice(bot), "answers": [SECRET], "current_index": 1}
    found = await interactions.find_by_prefix(db_session, bot_id=bot.id, task_id_prefix="ask1")
    assert found is not None and found.state == stored

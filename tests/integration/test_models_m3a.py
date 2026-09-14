import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import (
    TASK_KINDS,
    Announcement,
    Bot,
    InteractionState,
    RelayServer,
    User,
)


async def _bot(session: AsyncSession, key: str = "b1") -> Bot:
    user = User(login_name=f"creator-{key}", display_name="创建者")
    session.add(user)
    await session.flush()
    bot = Bot(
        bot_key=key,
        platform="wecom",
        name=key,
        created_by=user.id,
        model="vllm/claude-sonnet-4-6",
        working_dir="/data/skills/b1",
        credentials_enc="enc:v1:x",
    )
    session.add(bot)
    await session.commit()
    return bot


async def test_tables_indexes_and_task_kinds(db_session: AsyncSession) -> None:
    names = {
        r[0]
        for r in (
            await db_session.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname='public'")
            )
        ).all()
    }
    assert {
        "interaction_states_prefix_idx",
        "interaction_states_expires_idx",
        "announcements_active_idx",
    } <= names
    assert "card_action" in TASK_KINDS


async def test_interaction_state_defaults_checks_and_deferred_unique(
    db_session: AsyncSession,
) -> None:
    bot = await _bot(db_session)
    # 下面那些 rollback 会过期所有实例（与 expire_on_commit 无关），事后再取 .id 就成了同步懒加载。
    bot_id = bot.id
    st = InteractionState(bot_id=bot_id, kind="choice", scope_key=f"{bot_id}:zs", state={"a": 1})
    db_session.add(st)
    await db_session.commit()
    await db_session.refresh(st)
    assert st.status == "open" and st.expires_at is None and st.id is not None
    assert st.created_at is not None and st.updated_at is not None
    # 同 (kind, scope_key) 第二行：约束是 DEFERRABLE INITIALLY DEFERRED，flush 不报、commit 才报。
    db_session.add(
        InteractionState(bot_id=bot_id, kind="choice", scope_key=f"{bot_id}:zs", state={})
    )
    await db_session.flush()
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
    db_session.add(InteractionState(bot_id=bot_id, kind="poll", scope_key="x", state={}))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
    db_session.add(
        InteractionState(bot_id=bot_id, kind="relay_switch", scope_key="y", state={}, status="done")
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_announcement_scope_target_check_and_cascade(db_session: AsyncSession) -> None:
    bot = await _bot(db_session, "b2")
    relay = RelayServer(name="r-ann", host="relay.test", clawrelay_port=80, model_provider="claude")
    db_session.add(relay)
    await db_session.flush()
    # 同 test_models_m2：rollback 会过期实例，先把主键取出来再用。
    bot_id, relay_id = bot.id, relay.id
    db_session.add_all(
        [
            Announcement(scope="global", content="全局"),
            Announcement(scope="relay", relay_server_id=relay_id, content="按 relay"),
            Announcement(
                scope="bot",
                bot_id=bot_id,
                content="按 bot",
                start_at=datetime.now(UTC) - timedelta(hours=1),
                end_at=datetime.now(UTC) + timedelta(hours=1),
            ),
        ]
    )
    await db_session.commit()
    db_session.add(Announcement(scope="bot", content="缺 bot_id"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
    db_session.add(Announcement(scope="global", bot_id=bot_id, content="全局却带 bot"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
    db_session.add(
        InteractionState(bot_id=bot_id, kind="session_switch", scope_key=f"{bot_id}:g1", state={})
    )
    await db_session.commit()
    await db_session.delete(await db_session.get(Bot, bot_id))
    await db_session.commit()
    left = (await db_session.execute(select(Announcement.scope))).scalars().all()
    assert sorted(left) == ["global", "relay"]
    assert (await db_session.execute(select(InteractionState))).scalars().all() == []
    assert uuid.UUID(str(relay_id))

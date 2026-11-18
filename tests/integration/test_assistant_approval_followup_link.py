"""审批单挂到待跟进：当场提交与合单都记下；编号不对不挂也不拒；能补挂；决定时刷新进展。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import approvals, followup_tools, followups
from coreman.core.assistants.approvals import batch, service, tower
from coreman.core.db.models import AssistantApproval, AssistantFollowup
from coreman.core.timeutils import utcnow
from tests.integration.approval_helpers import CFG, assistant_turn, install, reply
from tests.integration.test_assistant_write_batches import away, release, second_reply, stage

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")
OLD = datetime(2026, 11, 2, tzinfo=UTC)


@pytest.fixture(autouse=True)
def tower_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tower, "config", lambda settings, target=tower.TARGET: CFG)


def _approval(bot_id: uuid.UUID, **kw: object) -> AssistantApproval:
    return AssistantApproval(
        bot_id=bot_id,
        tool="feishu_im_send_message",
        args_enc={"abstract": "私聊 → 李四", "calls": []},
        args_hash=uuid.uuid4().hex,
        request_hash=uuid.uuid4().hex,
        channel="p2p",
        external_id=f"cm-pa-{uuid.uuid4().hex}",
        expires_at=utcnow() + timedelta(hours=72),
        **kw,
    )


async def test_target_ignores_unknown_or_closed(db_session: AsyncSession) -> None:
    bot, _, _ = await assistant_turn(db_session)
    live = await followups.add(db_session, bot.id, category="promised", title="回复李四")
    gone = await followups.add(db_session, bot.id, category="remind", title="续约")
    await followups.close(db_session, bot.id, gone.seq)
    assert await followups.approval_target(db_session, bot.id, "F001") == live.id
    assert await followups.approval_target(db_session, bot.id, "F002") is None
    assert await followups.approval_target(db_session, bot.id, "F099") is None
    assert await followups.approval_target(db_session, bot.id, "") is None
    assert await followups.approval_target(db_session, bot.id, None) is None
    assert await followups.approval_target(db_session, bot.id, "not-a-label") is None


async def test_update_links_an_existing_approval(db_session: AsyncSession) -> None:
    bot, _, scope = await assistant_turn(db_session)
    item = await followups.add(db_session, bot.id, category="promised", title="回复李四")
    row = _approval(bot.id)
    db_session.add(row)
    await db_session.commit()
    out = await followup_tools.dispatch(
        db_session,
        None,
        scope,
        "followup_update",
        {"id": "F001", "approval_id": str(row.id)},  # type: ignore[arg-type]
    )
    await db_session.commit()
    assert "error" not in out
    await db_session.refresh(row)
    assert row.followup_id == item.id
    for bad_id in (str(uuid.uuid4()), "nope"):
        bad = await followup_tools.dispatch(
            db_session,
            None,
            scope,
            "followup_update",
            {"id": "F001", "approval_id": bad_id},  # type: ignore[arg-type]
        )
        assert bad["error"] == "approval_not_found"


async def test_update_error_leaves_the_link_unapplied(db_session: AsyncSession) -> None:
    bot, _, scope = await assistant_turn(db_session)
    await followups.add(db_session, bot.id, category="promised", title="回复李四")
    row = _approval(bot.id)
    db_session.add(row)
    await db_session.commit()
    out = await followup_tools.dispatch(
        db_session,
        None,
        scope,
        "followup_update",
        {"id": "F001", "approval_id": str(row.id), "handler": "job", "job": str(uuid.uuid4())},  # type: ignore[arg-type]
    )
    assert out["error"] == "job_not_found"
    await db_session.commit()
    await db_session.refresh(row)
    assert row.followup_id is None


async def test_decision_moves_the_followup(db_session: AsyncSession) -> None:
    bot, _, _ = await assistant_turn(db_session)
    item = await followups.add(db_session, bot.id, category="promised", title="回复李四")
    row = _approval(bot.id, followup_id=item.id, approval_id="ap-1")
    db_session.add(row)
    await db_session.execute(
        update(AssistantFollowup).where(AssistantFollowup.id == item.id).values(progress_at=OLD)
    )
    await db_session.commit()
    await service.decide(
        db_session,
        approval_id="ap-1",
        external_id=row.external_id,
        status="rejected",
        reason=None,
        decided_at=None,
    )
    await db_session.commit()
    assert (await followups.get(db_session, bot.id, item.seq)).progress_at > OLD


async def test_submit_records_the_followup_and_ignores_a_wrong_label(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, _, scope = await assistant_turn(db_session)
    item = await followups.add(db_session, bot.id, category="promised", title="回复李四")
    out = await approvals.submit(
        db_session,
        None,
        scope,
        "feishu_reply_message",
        reply(followup="F001"),
        cfg=CFG,  # type: ignore[arg-type]
    )
    other = await approvals.submit(
        db_session,
        None,  # type: ignore[arg-type]
        scope,
        "feishu_reply_message",
        {**reply(followup="F099"), "text": "另一句", "uuid": "u2"},
        cfg=CFG,
    )
    await db_session.commit()
    assert out["status"] == other["status"] == "pending_approval"
    linked = await db_session.get(AssistantApproval, out["approval_id"])
    unlinked = await db_session.get(AssistantApproval, other["approval_id"])
    assert linked is not None and linked.followup_id == item.id
    assert unlinked is not None and unlinked.followup_id is None


async def test_batched_approvals_record_the_followup(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, _, scope = await away(db_session)
    item = await followups.add(db_session, bot.id, category="promised", title="回复李四")
    await db_session.commit()
    await stage(db_session, scope, reply(followup="F001"), second_reply(followup="F001"))
    assert await release(db_engine, scope) == batch.TOWER
    rows = list(
        await db_session.scalars(
            select(AssistantApproval).execution_options(populate_existing=True)
        )
    )
    assert len(rows) == 2 and {row.followup_id for row in rows} == {item.id}

"""审批结果回合不出声、会话忙时直接记失败：结局已定却没投到的，scheduler 退避后重投，最终送达。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants.approvals import service
from coreman.core.assistants.kinds import APPROVAL_RESULT_KIND
from coreman.core.db.models import Bot, Task
from coreman.core.timeutils import utcnow
from tests.integration.approval_helpers import assistant_turn, decided, install, pending, queued

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")


async def end(session: AsyncSession, task: Task, status: str, code: str | None, ago: int) -> None:
    await session.execute(
        update(Task)
        .where(Task.id == task.id)
        .values(status=status, error_code=code, finished_at=utcnow() - timedelta(seconds=ago))
    )
    await session.commit()


async def test_a_result_turn_that_did_not_get_its_turn_is_delivered_again(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    _, _, scope = await assistant_turn(db_session)
    row = await pending(db_session, scope)
    await decided(db_session, row, "rejected", "先别发")
    (first,) = await queued(db_session, APPROVAL_RESULT_KIND)
    # 还在队列里：不重投。
    assert await service.redeliver(db_session, utcnow()) == 0
    # 会话忙，记了失败；退避（1 分钟）之前不重投，之后用新的去重键重投一次。
    await end(db_session, first, "failed", "session_busy", ago=30)
    assert await service.redeliver(db_session, utcnow()) == 0
    await end(db_session, first, "failed", "session_busy", ago=90)
    assert await service.redeliver(db_session, utcnow()) == 1
    await db_session.commit()
    first_retry = (await queued(db_session, APPROVAL_RESULT_KIND))[-1]
    assert first_retry.dedupe_key == f"assistant-approval:{row.id}:result:rejected:retry:1"
    assert first_retry.payload["approval_id"] == str(row.id)
    assert first_retry.payload["status"] == "rejected"
    # 重投的那一轮还在途：不再重复投。
    assert await service.redeliver(db_session, utcnow()) == 0
    # 又让路给所有者：退避加倍（2 分钟）。
    await end(db_session, first_retry, "cancelled", "platform_turn_yielded", ago=90)
    assert await service.redeliver(db_session, utcnow()) == 0
    await end(db_session, first_retry, "cancelled", "platform_turn_yielded", ago=150)
    assert await service.redeliver(db_session, utcnow()) == 1
    await db_session.commit()
    second_retry = (await queued(db_session, APPROVAL_RESULT_KIND))[-1]
    assert second_retry.dedupe_key.endswith(":retry:2")
    # 投到了：之后不再重投。
    await end(db_session, second_retry, "succeeded", None, ago=600)
    assert await service.redeliver(db_session, utcnow()) == 0


async def test_real_failures_and_stopped_assistants_are_not_redelivered(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    bot, _, scope = await assistant_turn(db_session)
    row = await pending(db_session, scope)
    await decided(db_session, row, "rejected")
    (first,) = await queued(db_session, APPROVAL_RESULT_KIND)
    # 不是「没轮到」，是这一轮本身被拒（结局对不上等）：不重投。
    await end(db_session, first, "cancelled", "approval_result_inactive", ago=600)
    assert await service.redeliver(db_session, utcnow()) == 0
    # 没轮到，但助理已停用：不重投。
    await end(db_session, first, "failed", "session_busy", ago=600)
    await db_session.execute(update(Bot).where(Bot.id == bot.id).values(enabled=False))
    await db_session.commit()
    assert await service.redeliver(db_session, utcnow()) == 0
    # scheduler 每轮都跑它。
    counts = await service.tick(db_session, utcnow())
    assert counts["assistant_approval_results_redelivered"] == 0

"""个人凭证提交、飞书就地授权完成：对应的缺口记为解决。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import gaps, sealed
from coreman.core.db.models import AssistantGap, FeishuAuthWait
from coreman.core.feishu_docs import resume
from coreman.core.personal_credentials import service
from coreman.core.timeutils import utcnow
from tests.integration.approval_helpers import assistant_turn
from tests.integration.test_personal_credentials_submit import VALUES, _opened

pytestmark = pytest.mark.usefixtures("assistant_master")


async def test_saved_credential_resolves_the_gap(db_session: AsyncSession) -> None:
    bot, user, _, cipher, row = await _opened(db_session)
    # 钩子不看机器人类型，有对得上的缺口就解决。
    gap = AssistantGap(
        bot_id=bot.id, seq=1, kind="credential", key="DEMO_PIN", need_enc="登录演示系统"
    )
    db_session.add(gap)
    await db_session.commit()
    await service.submit(db_session, cipher, row.id, actor_id=user.id, values=VALUES)
    await db_session.commit()
    await db_session.refresh(gap)
    assert gap.status == "resolved"


async def test_feishu_authorization_resolves_scope_gaps(db_session: AsyncSession) -> None:
    bot, owner, scope = await assistant_turn(db_session)
    await gaps.report(
        db_session,
        bot.id,
        kind="feishu_scope",
        key="read_messages",
        need="读群消息",
        followup_seqs=[],
    )
    db_session.add(
        FeishuAuthWait(
            bot_id=bot.id,
            user_id=owner.id,
            chat_id="oc_assistant",
            origin_task_id=scope.task.id,
            need="read_messages",
            status="waiting",
            expires_at=utcnow() + timedelta(minutes=30),
        )
    )
    await db_session.commit()
    assert await resume.tick(db_session, utcnow(), sealed.master()) == 1
    await db_session.commit()
    assert (await gaps.get(db_session, bot.id, 1)).status == "resolved"


async def _boom(*args, **kwargs):
    raise RuntimeError("boom")


async def test_failing_gap_resolution_does_not_lose_the_saved_credential(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, user, _, cipher, row = await _opened(db_session)
    monkeypatch.setattr(gaps, "resolve", _boom)
    result = await service.submit(db_session, cipher, row.id, actor_id=user.id, values=VALUES)
    await db_session.commit()
    assert result.status == "saved"


async def test_failing_gap_resolution_does_not_abort_the_resume_tick(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, owner, scope = await assistant_turn(db_session)
    wait = FeishuAuthWait(
        bot_id=bot.id,
        user_id=owner.id,
        chat_id="oc_assistant",
        origin_task_id=scope.task.id,
        need="read_messages",
        status="waiting",
        expires_at=utcnow() + timedelta(minutes=30),
    )
    db_session.add(wait)
    await db_session.commit()
    monkeypatch.setattr(gaps, "resolve", _boom)
    assert await resume.tick(db_session, utcnow(), sealed.master()) == 1

"""带入站事件的审批结果回合也是无人在场的一轮：让路、不挂云签名代理 / 业务系统目录 / 个人凭证、
不得改动置顶页。入站事件是提交那一轮留下的，所有者此刻并不在场。"""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import kinds, notes, notes_tools, platform_turns
from coreman.core.assistants.approvals import service
from coreman.core.assistants.kinds import (
    APPROVAL_EXECUTE_KIND,
    APPROVAL_RESULT_KIND,
    BOT_HELP_KIND,
    HANDOVER_KIND,
    ORIGIN_KINDS,
)
from coreman.core.cloud_proxy import policy as cloud_policy
from coreman.core.db.models import Bot, InboundEvent, Task
from coreman.runtime.worker.assistant_approvals import AssistantApprovalResultHandler
from coreman.runtime.worker.chat import credentials
from tests.integration.approval_helpers import assistant_turn
from tests.integration.credential_helpers import VALUES
from tests.integration.test_assistant_approval_result import _row
from tests.integration.test_assistant_notes_tools import call
from tests.integration.test_assistant_platform_turn_capabilities import equipped, mounted, sent
from tests.integration.test_assistant_platform_turn_handler import assistant, run, streams_of
from tests.integration.test_assistant_platform_turn_yield import fresh, owner_message
from tests.integration.test_cron_handler import claim
from tests.integration.worker_helpers import turn_block

pytestmark = pytest.mark.usefixtures("assistant_master")


async def result_turn(session: AsyncSession, bot: Bot) -> Task:
    """所有者私聊那一轮里提交的单有了结局：结果回合带着那一轮的入站事件，已认领。"""
    event = await session.scalar(
        select(InboundEvent).where(InboundEvent.bot_id == bot.id).order_by(InboundEvent.id).limit(1)
    )
    assert event is not None
    row = _row(
        bot.id,
        "rejected",
        origin_event_id=event.id,
        session_key="oc_private",
        reason_enc="改客气点",
    )
    session.add(row)
    await session.flush()
    assert await service.deliver(session, row, bot) is True
    await session.commit()
    claimed = await claim(session)
    assert claimed.kind == APPROVAL_RESULT_KIND and claimed.inbound_event_id == event.id
    return claimed


def test_unattended_goes_by_the_kind_not_by_the_inbound_event() -> None:
    for kind in ORIGIN_KINDS:
        assert kinds.unattended(kind)
        assert platform_turns.unattended(Task(kind=kind, inbound_event_id=None))
        assert platform_turns.unattended(Task(kind=kind, inbound_event_id=3))
    for kind in ("chat", APPROVAL_EXECUTE_KIND, BOT_HELP_KIND, "cron"):
        assert not kinds.unattended(kind)
        assert not platform_turns.unattended(Task(kind=kind, inbound_event_id=3))
    # 「平台来源」（没有入站事件 → 能力令牌与会话围栏那一套）的含义不变。
    assert not platform_turns.is_platform(Task(kind=APPROVAL_RESULT_KIND, inbound_event_id=3))
    assert platform_turns.is_platform(Task(kind=HANDOVER_KIND, inbound_event_id=None))


async def test_it_yields_to_the_owners_message_instead_of_cancelling_it(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await assistant(db_session)
    turn = await result_turn(db_session, bot)
    message = await owner_message(db_session, bot)
    done, relay = await run(db_engine, db_session, turn, AssistantApprovalResultHandler())
    assert done.status == "queued" and done.cancel_requested_at is None
    assert relay.requests == [] and await streams_of(db_session, turn) == []
    kept = await fresh(db_session, message)
    assert kept.cancel_requested_at is None and kept.status == "claimed"


async def test_it_gets_no_cloud_proxy_catalog_or_credentials(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = await equipped(db_session, monkeypatch)
    turn = await result_turn(db_session, bot)
    request = await sent(db_engine, turn, AssistantApprovalResultHandler())
    env = request["env_vars"]
    assert mounted(env) == set()
    assert not set(VALUES.values()) & set(env.values())
    # 找谁审批的名单照常下发（个人工具照常可用见 tests/api/test_assistant_tool_groups.py）。
    assert cloud_policy.APPROVERS_ENV in env
    assert credentials.OWNER_ABSENT in turn_block(request)


async def test_it_cannot_touch_pinned_pages(db_session: AsyncSession) -> None:
    bot, _, owner = await assistant_turn(db_session)
    rule = {"path": "rules/tone.md", "summary": "口吻"}
    pinned = await call(db_session, owner, "notes_write", body="先给结论", pinned=True, **rule)
    assert pinned["pinned"] is True
    # 同一条入站事件、同一个 scope，只是任务类型是审批结果回合。
    assert owner.event is not None
    result = replace(
        owner,
        task=Task(
            id=owner.task.id,
            bot_id=bot.id,
            kind=APPROVAL_RESULT_KIND,
            inbound_event_id=owner.event.id,
        ),
    )
    assert result.platform is False and result.unattended is True and owner.unattended is False
    refused = {"error": "pin_requires_owner", "hint": notes_tools.PIN_HINT}
    out = await call(db_session, result, "notes_write", body="改掉", version=1, **rule)
    assert out == refused
    out = await call(
        db_session, result, "notes_write", path="rules/new.md", body="x", summary="", pinned=True
    )
    assert out == refused
    assert await call(db_session, result, "notes_delete", path="rules/tone.md") == refused
    page = await notes.read(db_session, bot.id, "rules/tone.md")
    assert page is not None and page.version == 1
    # 不置顶的页照常能写。
    saved = await call(db_session, result, "notes_write", path="log/a.md", body="记", summary="")
    assert saved["version"] == 1

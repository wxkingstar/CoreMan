"""「不开 G003」由平台截下、不经模型；「开通 G003」开一轮在场轮次，本轮块里附着怎么开。"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import followups, gaps
from coreman.core.db.models import Bot, Task
from tests.fakes.fake_relay import FakeRelay
from tests.integration.assistant_helpers import identified
from tests.integration.lifecycle_helpers import active_assistant, install
from tests.integration.shift_helpers import last_sent, say
from tests.integration.test_chat_handler import chat_task, run
from tests.integration.worker_helpers import turn_block

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")


@pytest.fixture(autouse=True)
def installed(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)


async def _seeded(
    db_session: AsyncSession, kind: str = "feishu_scope", key: str = "im:message"
) -> Bot:
    bot, _, _ = await active_assistant(db_session)
    await identified(db_session, bot)
    await followups.add(db_session, bot.id, category="promised", title="整理群里的需求")
    await gaps.report(db_session, bot.id, kind=kind, key=key, need="读群消息", followup_seqs=[1])
    await db_session.commit()
    return bot


async def test_decline_is_intercepted(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    bot = await _seeded(db_session)
    done, relay = await say(db_engine, db_session, bot, "不开 G001")
    assert done.result == {"assistant": "gap_declined"} and relay.requests == []
    assert (await gaps.get(db_session, bot.id, 1)).status == "declined"
    sent = await last_sent(db_session, bot)
    assert sent is not None and "G001" in sent.payload["markdown"]


async def test_decline_by_someone_else_does_nothing(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot = await _seeded(db_session)
    done, relay = await say(db_engine, db_session, bot, "不开 G001", sender="stranger_pid")
    assert done.result == {"assistant": "board_not_owner"} and relay.requests == []
    assert (await gaps.get(db_session, bot.id, 1)).status == "open"


async def test_decline_of_an_unknown_gap(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    bot = await _seeded(db_session)
    done, relay = await say(db_engine, db_session, bot, "不开 G009")
    assert done.result == {"assistant": "gap_not_found"} and relay.requests == []
    sent = await last_sent(db_session, bot)
    assert sent is not None and "没有找到 G009" in sent.payload["markdown"]


async def test_open_starts_an_attended_turn(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot = await _seeded(db_session)
    done, relay = await say(db_engine, db_session, bot, "开通 G001")
    assert done.status == "succeeded" and len(relay.requests) == 1
    block = turn_block(relay.requests[0])
    assert "要开通缺口 G001" in block and "feishu_authorize" in block and "F001" in block


async def test_open_reopens_a_declined_gap(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot = await _seeded(db_session)
    await gaps.decline(db_session, bot.id, 1)
    await db_session.commit()
    done, relay = await say(db_engine, db_session, bot, "开通 G001")
    assert done.status == "succeeded" and len(relay.requests) == 1
    bot_id = bot.id
    db_session.expire_all()
    assert (await gaps.get(db_session, bot_id, 1)).status == "open"


# 「开通 G001」「现在处理 F001」只认所有者在自己私聊里发的：别人私聊、群里（含所有者在群里）
# 发的不重新打开缺口、不附说明、不把记下的需要或标题带进这一轮。
NOT_THE_OWNER_DM = [
    pytest.param({"sender": "stranger_pid"}, id="stranger-dm"),
    pytest.param({"chat_type": "group", "chat_id": "oc_group"}, id="owner-in-group"),
    pytest.param(
        {"sender": "stranger_pid", "chat_type": "group", "chat_id": "oc_group"},
        id="stranger-in-group",
    ),
]


@pytest.mark.parametrize("where", NOT_THE_OWNER_DM)
async def test_open_only_from_the_owner_dm(
    db_engine: AsyncEngine, db_session: AsyncSession, where: dict[str, str]
) -> None:
    bot = await _seeded(db_session)
    await gaps.decline(db_session, bot.id, 1)
    await db_session.commit()
    bot_id = bot.id
    _, relay = await say(db_engine, db_session, bot, "开通 G001", **where)
    db_session.expire_all()
    assert (await gaps.get(db_session, bot_id, 1)).status == "declined"
    for request in relay.requests:
        block = turn_block(request)
        assert "要开通缺口" not in block and "读群消息" not in block and "im:message" not in block


@pytest.mark.parametrize("where", NOT_THE_OWNER_DM)
async def test_handle_now_only_from_the_owner_dm(
    db_engine: AsyncEngine, db_session: AsyncSession, where: dict[str, str]
) -> None:
    bot = await _seeded(db_session)
    _, relay = await say(db_engine, db_session, bot, "现在处理 F001", **where)
    for request in relay.requests:
        block = turn_block(request)
        assert "要现在处理待跟进" not in block and "整理群里的需求" not in block


async def test_handle_now_from_the_owner_dm(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot = await _seeded(db_session)
    _, relay = await say(db_engine, db_session, bot, "现在处理 F001")
    assert len(relay.requests) == 1
    block = turn_block(relay.requests[0])
    assert "要现在处理待跟进 F001" in block and "整理群里的需求" in block


@pytest.mark.parametrize("text", ["开通 G001", "不开 G001", "现在处理 F001"])
async def test_a_queued_button_press_is_not_merged(
    db_engine: AsyncEngine, db_session: AsyncSession, text: str
) -> None:
    """按钮按下发出的那句话要有自己的一轮（截下或附上说明），不能被并进正在跑的那一轮。"""
    bot = await _seeded(db_session)
    kw: dict[str, Any] = {"sender": "owner_pid", "chat_type": "single", "chat_id": "oc_private"}
    head = await chat_task(db_session, bot, "帮我看看今天的消息", **kw)
    head.status = "running"
    await db_session.commit()
    follow = await chat_task(db_session, bot, "顺便看下日程", **kw)
    follow.status = "running"
    await db_session.commit()
    pressed = await chat_task(db_session, bot, text, **kw)
    for row in (follow, pressed):
        row.status, row.claimed_by = "queued", None
    await db_session.commit()
    relay = FakeRelay("normal")
    await run(db_engine, head, relay)
    merged = await db_session.get(Task, follow.id, populate_existing=True)
    assert merged is not None and merged.error_code == "merged"
    left = await db_session.get(Task, pressed.id, populate_existing=True)
    assert left is not None and left.status == "queued" and left.error_code is None
    assert text not in str(relay.requests)

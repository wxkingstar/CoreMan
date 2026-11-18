"""开轮：能力清单进本轮块、不进稳定段；级别按这一轮实际拿到的说；缺口只在级别升上去时解决。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import capabilities, followups, gaps, sealed
from coreman.core.db.models import AssistantInbox, BotSystemGrant, BusinessSystem, User
from tests.integration.assistant_helpers import identified
from tests.integration.lifecycle_helpers import active_assistant, install
from tests.integration.shift_helpers import say
from tests.integration.worker_helpers import turn_block

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")
OPENAPI = "https://orders.example.com/openapi.json"
CONFIGURED = SimpleNamespace(cloud_proxy_ready=True, cloud_proxy_policy=None)


@pytest.fixture(autouse=True)
def installed(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)


async def _reminders(session: AsyncSession, bot_id) -> list[dict]:  # type: ignore[no-untyped-def]
    rows = list(
        await session.scalars(
            select(AssistantInbox).where(
                AssistantInbox.bot_id == bot_id, AssistantInbox.kind == "reminder"
            )
        )
    )
    await sealed.open_rows(session, rows)
    return [row.summary_enc for row in rows]


async def _grant(session: AsyncSession, bot_id, **system) -> None:  # type: ignore[no-untyped-def]
    session.add(BusinessSystem(key="orders", name="订单系统", allowed_bot_ids=None, **system))
    await session.flush()
    session.add(BotSystemGrant(bot_id=bot_id, system_key="orders"))
    await session.flush()


async def _report(session: AsyncSession, bot_id, kind: str, key: str) -> None:  # type: ignore[no-untyped-def]
    """和 gap_report 工具一样：报的时候按现在的授权记级别。"""
    await followups.add(session, bot_id, category="promised", title="改订单")
    await gaps.report(
        session,
        bot_id,
        kind=kind,
        key=key,
        need="改订单",
        followup_seqs=[1],
        seen_rank=await capabilities.current_rank(
            session, bot_id, kind, key, deployment=CONFIGURED
        ),
    )


async def test_note_in_turn_block_and_gap_resolved(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """报缺口时没有这个系统，之后授权了：下一轮解决。"""
    bot, _, _ = await active_assistant(db_session)
    await identified(db_session, bot)
    await _report(db_session, bot.id, "business_system", "orders")
    await _grant(db_session, bot.id)
    await db_session.commit()
    done, relay = await say(db_engine, db_session, bot, "你好")
    assert done.status == "succeeded" and len(relay.requests) == 1
    block = turn_block(relay.requests[0])
    assert capabilities.HEAD in block and "订单系统（orders）可读写" in block
    assert capabilities.HEAD not in relay.requests[0]["messages"][0]["content"]
    assert (await gaps.get(db_session, bot.id, 1)).status == "resolved"


async def test_read_gap_stays_open_until_write_is_granted(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """报缺口时已经能读（要的是写）：下一轮不算解决；授权写之后解决一次、只入一条提醒。"""
    bot, _, _ = await active_assistant(db_session)
    await identified(db_session, bot)
    await _grant(db_session, bot.id, token_delivery="proxy", openapi_url=OPENAPI)
    await _report(db_session, bot.id, "business_system", "orders")
    await db_session.commit()
    assert (await gaps.get(db_session, bot.id, 1)).seen_rank == 1
    await say(db_engine, db_session, bot, "你好")
    assert (await gaps.get(db_session, bot.id, 1)).status == "open"
    await db_session.execute(
        update(BotSystemGrant).where(BotSystemGrant.bot_id == bot.id).values(allow_write=True)
    )
    await db_session.commit()
    await say(db_engine, db_session, bot, "再看看")
    await say(db_engine, db_session, bot, "还在吗")
    assert (await gaps.get(db_session, bot.id, 1)).status == "resolved"
    told = [r for r in await _reminders(db_session, bot.id) if r["reminder"] == "gap_resolved"]
    assert len(told) == 1 and told[0]["gap"] == "G001"


async def test_level_follows_what_this_turn_got(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """所有者没有邮箱、令牌签不出来：清单说本轮没下发，不说可读写；授权还在，所以不登记成缺口。"""
    bot, owner, _ = await active_assistant(db_session)
    await identified(db_session, bot)
    await _grant(db_session, bot.id)
    await db_session.execute(update(User).where(User.id == owner.id).values(email=None))
    await db_session.commit()
    _, relay = await say(db_engine, db_session, bot, "你好")
    block = turn_block(relay.requests[0])
    assert "订单系统（orders）本轮没下发" in block and "可读写" not in block


async def test_cloud_gap_reported_while_configured_stays_open(db_session: AsyncSession) -> None:
    bot, _, _ = await active_assistant(db_session)
    await _report(db_session, bot.id, "cloud_product", "slb")
    assert (await gaps.get(db_session, bot.id, 1)).seen_rank == 1
    await capabilities.apply(
        db_session,
        bot.id,
        mode="attended",
        mcp_supported=False,
        mounted=False,
        cloud_attached=True,
        deployment=CONFIGURED,
    )
    assert (await gaps.get(db_session, bot.id, 1)).status == "open"


async def test_withheld_turn_resolves_nothing(db_session: AsyncSession) -> None:
    """冷启动、告警这类轮次不比对：等下一个正常的轮次。"""
    bot, _, _ = await active_assistant(db_session)
    off = SimpleNamespace(cloud_proxy_ready=False, cloud_proxy_policy=None)
    await followups.add(db_session, bot.id, category="promised", title="改解析")
    await gaps.report(
        db_session, bot.id, kind="cloud_product", key="alidns", need="改解析", followup_seqs=[1]
    )
    assert (
        await capabilities.current_rank(
            db_session, bot.id, "cloud_product", "alidns", deployment=off
        )
        == 0
    )
    note = await capabilities.apply(
        db_session,
        bot.id,
        mode="withheld",
        mcp_supported=False,
        mounted=False,
        cloud_attached=False,
        deployment=CONFIGURED,
    )
    assert note == capabilities.WITHHELD
    assert (await gaps.get(db_session, bot.id, 1)).status == "open"
    await capabilities.apply(
        db_session,
        bot.id,
        mode="attended",
        mcp_supported=False,
        mounted=False,
        cloud_attached=True,
        deployment=CONFIGURED,
    )
    assert (await gaps.get(db_session, bot.id, 1)).status == "resolved"

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bus import instances, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.chat import interactions, sessions
from coreman.core.chat.rate_limit import quota_table
from coreman.core.db.models import (
    AuditLog,
    Bot,
    BotMember,
    InboundEvent,
    InteractionState,
    OutboxItem,
    RelayServer,
    Team,
    User,
    UserIdentity,
)
from coreman.core.i18n.messages import msg
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from coreman.runtime.worker.relay_switch import RelaySwitchHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.test_chat_handler import chat_task, run, stream_of
from tests.integration.worker_helpers import build_ctx, seed_bot


async def _second_relay(session: AsyncSession, *, pct7=Decimal("5")) -> RelayServer:  # type: ignore[no-untyped-def]
    r = RelayServer(name="r-idle", host="idle.test", clawrelay_port=80, model_provider="claude")
    r.rate_limit_7d_used_pct = pct7
    r.rate_limit_5h_used_pct = Decimal("10")
    r.rate_limit_probed_at = datetime.now(UTC)
    session.add(r)
    await session.commit()
    return r


async def _creator_identity(session: AsyncSession, bot: Bot, platform_user_id: str = "zs") -> User:
    creator = await session.get(User, bot.created_by)
    assert creator
    session.add(
        UserIdentity(user_id=creator.id, platform="wecom", platform_user_id=platform_user_id)
    )
    await session.commit()
    return creator


async def test_rate_limited_reply_gets_table_and_offer_for_admin(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, relay, _ = await seed_bot(db_session)
    relay.rate_limit_5h_used_pct, relay.rate_limit_7d_used_pct = Decimal("100"), Decimal("80")
    relay.rate_limit_probed_at = datetime.now(UTC)
    idle = await _second_relay(db_session)
    await _creator_identity(db_session, bot)
    fake = FakeRelay("rate_limit")
    t = await chat_task(db_session, bot, "跑一下")
    await run(db_engine, t, fake)
    s = await stream_of(db_session, t.id)
    assert "**📊 服务器额度总览**" in s.final_text and "| sales_bot" not in s.final_text
    assert f"| {relay.name} ⭐ |" in s.final_text and f"| {idle.name} |" in s.final_text
    st = await interactions.get_open(
        db_session, kind="relay_switch", scope_key=interactions.choice_scope(bot.id, "zs")
    )
    assert (
        st is not None
        and st.task_id_prefix
        and st.task_id_prefix.startswith("ratelimit_switch@sales_bot@zs@")
    )
    assert st.expires_at and st.expires_at - datetime.now(UTC) < timedelta(minutes=31)
    assert (
        st.state["target_relay_id"] == str(idle.id)
        and st.state["current_name"] == relay.name
        and st.state["target_pct_7d"] == 5.0
    )
    card = s.pending_card
    assert (
        card
        and card["checkbox"]["question_key"] == "ratelimit_switch_choice"
        and card["task_id"] == f"{st.task_id_prefix}@0"
    )
    assert card["main_title"]["desc"].endswith("（7天使用率 5%）？")
    # 非管理员：只有额度表，没有卡片与状态。
    t2 = await chat_task(db_session, bot, "再跑", sender="nobody")
    await run(db_engine, t2, fake)
    s2 = await stream_of(db_session, t2.id)
    assert "**📊 服务器额度总览**" in s2.final_text and s2.pending_card is None
    assert len((await db_session.execute(select(InteractionState))).scalars().all()) == 1


async def test_warning_without_limit_and_no_candidate(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, relay, _ = await seed_bot(db_session)
    relay.rate_limit_5h_used_pct, relay.rate_limit_7d_used_pct = Decimal("91"), Decimal("10")
    relay.rate_limit_probed_at = datetime.now(UTC)
    await db_session.commit()
    await _creator_identity(db_session, bot)
    t = await chat_task(db_session, bot, "你好")
    await run(db_engine, t, FakeRelay("normal"))
    s = await stream_of(db_session, t.id)
    assert "⚠️ **额度提醒**：本运行时 5 小时额度已用 91%" in s.final_text
    assert "后重置" in s.final_text
    assert s.final_text.endswith(msg("done_suffix"))  # 额度提醒插在 ✅ 之前，✅ 仍在末尾
    t2 = await chat_task(db_session, bot, "撞限")
    await run(db_engine, t2, FakeRelay("rate_limit"))  # 没有可切换的候选：表有、卡无
    s2 = await stream_of(db_session, t2.id)
    assert "📊 服务器额度总览" in s2.final_text and s2.pending_card is None
    assert (await db_session.execute(select(InteractionState))).scalars().all() == []


async def test_reaper_wins_before_rate_limit_finalization_without_orphan_offer(
    db_engine, db_session
):
    bot, _, _ = await seed_bot(db_session)
    await _second_relay(db_session)
    await _creator_identity(db_session, bot)
    task = await chat_task(db_session, bot, "撞限后被收尾")
    fake = FakeRelay("rate_limit")

    class ReapedBeforeFinalize(ChatTaskHandler):
        async def _finalize(self, ctx, pre, out):
            async with ctx.session_factory() as session:
                await tasks.finish(session, ctx.task.id, status="failed", error_code="worker_lost")
                await session.commit()
            await super()._finalize(ctx, pre, out)

    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _: fake.client())
    await ReapedBeforeFinalize().run(ctx)
    await ctx.chat_logs.drain(5)
    assert (await db_session.execute(select(InteractionState))).scalars().all() == []
    assert (await db_session.execute(select(OutboxItem))).scalars().all() == []


async def _switch_task(
    session: AsyncSession, bot: Bot, st: InteractionState, *, user_id, target_id
):  # type: ignore[no-untyped-def]
    ev = InboundEvent(
        bot_id=bot.id,
        platform="wecom",
        platform_msg_id=f"e-{uuid.uuid4()}",
        kind="card_action",
        chat_type="single",
        chat_id="zs",
        sender_platform_user_id="zs",
        payload={},
        reply_context={
            "gateway_instance": "gw",
            "req_id": "r",
            "chat_type": "single",
            "chat_id": "zs",
        },
    )
    session.add(ev)
    await session.flush()
    t = await tasks.enqueue(
        session,
        NewTask(
            bot_id=bot.id,
            kind="relay_switch",
            lane="fast",
            payload={
                "state_id": str(st.id),
                "bot_key": bot.bot_key,
                "platform_user_id": "zs",
                "user_id": str(user_id) if user_id else None,
                "chat_id": "zs",
                "chat_type": "single",
                "target_relay_server_id": str(target_id),
                "current_name": "r1",
                "target_name": "r-idle",
            },
            session_key="zs",
            inbound_event_id=ev.id,
            dedupe_key=f"relay_switch:{st.id}",
        ),
    )
    await session.commit()
    assert t
    await instances.register(
        session, instance_id="worker-test", service="worker", version="dev", capacity=8
    )
    claimed = await tasks.claim(session, lane="fast", instance_id="worker-test")
    await session.commit()
    assert claimed
    return claimed


async def test_relay_switch_handler_switches_and_notifies(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, relay, _ = await seed_bot(db_session)
    idle = await _second_relay(db_session)
    creator = await _creator_identity(db_session, bot)
    await sessions.get_or_create(
        db_session,
        bot_id=bot.id,
        session_key="zs",
        backend="claude",
        ttl_hours=72,
        speaker_user_id=creator.id,
    )
    st = await interactions.open_state(
        db_session,
        bot_id=bot.id,
        kind="relay_switch",
        scope_key=interactions.choice_scope(bot.id, "zs"),
        state={
            "idx": 0,
            "platform_user_id": "zs",
            "user_id": str(creator.id),
            "current_relay_id": str(relay.id),
            "current_name": "r1",
            "target_relay_id": str(idle.id),
            "target_name": "r-idle",
            "target_pct_7d": 5.0,
            "chat_id": "zs",
            "chat_type": "single",
        },
        task_id_prefix="ratelimit_switch@sales_bot@zs@1",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    await interactions.set_status(db_session, st.id, "submitted")
    await db_session.commit()
    t = await _switch_task(db_session, bot, st, user_id=creator.id, target_id=idle.id)
    await RelaySwitchHandler().run(build_ctx(db_engine, t))
    await db_session.refresh(bot)
    assert bot.relay_server_id == idle.id and bot.model == "vllm/claude-sonnet-4-6"
    assert await sessions.list_for_bot(db_session, bot.id) == []
    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "bot.switch_relay"))
    ).scalar_one()
    assert audit.actor_id == creator.id
    items = (await db_session.execute(select(OutboxItem))).scalars().all()
    assert items[-1].payload["markdown"] == msg(
        "relay_switch_ok",
        current="r1",
        target="r-idle",
        detail=msg("relay_switch_detail", model="vllm/claude-sonnet-4-6"),
    )
    assert (await db_session.execute(select(InteractionState))).scalars().all() == []
    row = await tasks.get(db_session, t.id)
    assert row and row.status == "succeeded"


async def test_relay_switch_handler_reports_policy_failure(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, relay, _ = await seed_bot(db_session)
    idle = await _second_relay(db_session)
    # 别的团队的私有实例。team_id 有外键，必须落一行真团队（原计划里的随机 UUID 进不了库）。
    other_team = Team(slug="other", name_zh="别的团队")
    db_session.add(other_team)
    await db_session.flush()
    idle.team_id = other_team.id
    member = User(login_name="m", display_name="m")
    db_session.add(member)
    await db_session.flush()
    db_session.add(BotMember(bot_id=bot.id, user_id=member.id))
    db_session.add(UserIdentity(user_id=member.id, platform="wecom", platform_user_id="zs"))
    await db_session.commit()
    st = await interactions.open_state(
        db_session,
        bot_id=bot.id,
        kind="relay_switch",
        scope_key=interactions.choice_scope(bot.id, "zs"),
        state={
            "idx": 0,
            "platform_user_id": "zs",
            "user_id": str(member.id),
            "current_relay_id": str(relay.id),
            "current_name": "r1",
            "target_relay_id": str(idle.id),
            "target_name": "r-idle",
            "target_pct_7d": 5.0,
            "chat_id": "zs",
            "chat_type": "single",
        },
        task_id_prefix="ratelimit_switch@sales_bot@zs@2",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    await db_session.commit()
    t = await _switch_task(db_session, bot, st, user_id=member.id, target_id=idle.id)
    await RelaySwitchHandler().run(build_ctx(db_engine, t))
    await db_session.refresh(bot)
    assert bot.relay_server_id == relay.id
    items = (await db_session.execute(select(OutboxItem))).scalars().all()
    assert items[-1].payload["markdown"] == msg(
        "relay_switch_failed",
        current="r1",
        target="r-idle",
        detail="目标运行时不属于本团队或公共池",
    )
    assert quota_table([relay], current_id=relay.id, now=datetime.now(UTC))

"""Claude 后台阶段：进入、观察、本人接着发、别人排队、停止、重置与看护。"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bus import instances, streams, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.chat import background_runs
from coreman.core.db.models import (
    BackgroundRun,
    Bot,
    BusinessSystem,
    BusinessTokenIssue,
    ChatLog,
    InboundEvent,
    OutboxItem,
    RelayServer,
    RuntimeNode,
    Task,
    TaskStream,
    User,
    UserIdentity,
)
from coreman.core.i18n.messages import msg
from coreman.core.prompting.defaults import BACKGROUND_TASKS_RULE
from coreman.core.relay.client import RelayClient
from coreman.runtime.worker.background_watch import (
    BackgroundControlHandler,
    BackgroundWatchHandler,
    handle_card,
)
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from tests.fakes.fake_relay import DONE, FINISH, USAGE, _chunk, _text
from tests.integration.worker_helpers import build_ctx, seed_bot

OWNER = "ou_owner"
DEADLINE = 4102444800  # 2100-01-01


def runtime(value: dict[str, Any]) -> str:
    return "data: " + json.dumps({"x_runtime": value, "choices": []}, ensure_ascii=False) + "\n\n"


def bg_task(tid: str, status: str = "running", **extra: Any) -> dict[str, Any]:
    return {
        "id": tid,
        "description": f"任务{tid}",
        "kind": "agent",
        "status": status,
        "started_at": int(datetime.now(UTC).timestamp()) - 60,
        **extra,
    }


class LiveRelay:
    """按路径回放脚本的假驱动：对话、attach 各一串脚本，整数表示返回该状态码。"""

    def __init__(
        self,
        *,
        chats: list[list[str] | tuple[int, str]] | None = None,
        attaches: list[list[str] | tuple[int, str]] | None = None,
    ) -> None:
        self.chats = list(chats or [])
        self.attaches = list(attaches or [])
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def client(self, _relay: RelayServer | None = None) -> RelayClient:
        return RelayClient(
            "http://relay.test",
            http=httpx.AsyncClient(
                base_url="http://relay.test", transport=httpx.MockTransport(self._handle)
            ),
        )

    def bodies(self, path: str) -> list[dict[str, Any]]:
        return [body for p, body in self.requests if p == path]

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}")
        self.requests.append((path, body))
        if path in ("/live/close", "/live/stop_task"):
            return httpx.Response(200, json={"ok": True})
        queue = self.chats if path == "/v1/chat/completions" else self.attaches
        script = queue.pop(0) if queue else (404, "not_live")
        if isinstance(script, tuple):
            code, error = script
            return httpx.Response(code, json={"error": {"code": error}})
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content="".join(script).encode()
        )


def main_turn_with_background() -> list[str]:
    return [
        runtime({"background_tasks": [bg_task("a")]}),
        _text("已在后台开始 2 项"),
        runtime({"background": {"tasks": [bg_task("a"), bg_task("b")], "deadline": DEADLINE}}),
        FINISH,
        _chunk(None, usage=USAGE),
        DONE,
    ]


def plain_turn(text: str = "好的") -> list[str]:
    return [_text(text), FINISH, _chunk(None, usage=USAGE), DONE]


async def feishu_bot(session: AsyncSession, *, capable: bool = True) -> tuple[Bot, RelayServer]:
    bot, relay, _ = await seed_bot(session)
    bot.platform = "feishu"
    node = await session.get(RuntimeNode, relay.runtime_node_id)
    assert node is not None
    claude = {"installed": True, "login": "ready"}
    if capable:
        claude[background_runs.CAPABILITY] = True  # type: ignore[assignment]
    node.capabilities = {"claude": claude, "codex": {"installed": True, "login": "ready"}}
    node.heartbeat_at = datetime.now(UTC)
    user = User(login_name="owner", display_name="提问人", email="owner@example.test")
    session.add_all([user, BusinessSystem(key="erp", name="ERP", default_for_all_bots=True)])
    await session.flush()
    session.add(UserIdentity(user_id=user.id, platform="feishu", platform_user_id=OWNER))
    await session.commit()
    return bot, relay


async def feishu_task(
    session: AsyncSession,
    bot: Bot,
    text: str,
    *,
    sender: str = OWNER,
    chat_type: str = "single",
    chat_id: str = "oc_chat",
    claim: bool = True,
) -> Task:
    message_id = f"om_{uuid.uuid4().hex[:12]}"
    m = {
        "platform": "feishu",
        "kind": "message",
        "chat_type": chat_type,
        "chat_id": chat_id,
        "sender": {"platform_user_id": sender, "open_id": None},
        "message_id": message_id,
        "mentions_bot": True,
        "parts": [{"type": "text", "text": text}],
        "card_action": None,
        "raw": {},
    }
    ev = InboundEvent(
        bot_id=bot.id,
        platform="feishu",
        platform_msg_id=message_id,
        kind="message",
        chat_type=chat_type,
        chat_id=chat_id,
        sender_platform_user_id=sender,
        payload=m,
        reply_context={"chat_id": chat_id, "chat_type": chat_type, "message_id": message_id},
    )
    session.add(ev)
    await session.flush()
    t = await tasks.enqueue(
        session,
        NewTask(
            bot_id=bot.id,
            kind="chat",
            payload={"message": m, "bot_key": bot.bot_key, "platform_user_id": sender},
            session_key=chat_id,
            inbound_event_id=ev.id,
        ),
    )
    await session.commit()
    assert t is not None
    if not claim:
        return t
    return await claim_task(session, t.id)


async def claim_task(session: AsyncSession, task_id: int, lane: str = "normal") -> Task:
    await instances.register(
        session, instance_id="worker-test", service="worker", version="dev", capacity=8
    )
    while True:
        claimed = await tasks.claim(session, lane=lane, instance_id="worker-test")
        assert claimed is not None
        if claimed.id == task_id:
            await session.commit()
            return claimed
        # 前面的用例步骤顺带建的任务（观察任务等）：这里不跑它们。
        await tasks.finish(session, claimed.id, status="cancelled")
        await session.commit()


async def run_chat(engine: AsyncEngine, task: Task, relay: LiveRelay) -> None:
    ctx = build_ctx(engine, task, relay_client_factory=relay.client)
    handler = ChatTaskHandler()
    handler.LIVE_RETRY_SECONDS = 0.01
    await handler.run(ctx)
    await ctx.chat_logs.drain(5)


async def outbox_items(session: AsyncSession) -> list[OutboxItem]:
    return list((await session.execute(select(OutboxItem).order_by(OutboxItem.id))).scalars().all())


async def make_run(
    session: AsyncSession,
    bot: Bot,
    relay: RelayServer,
    *,
    tasks_: list[dict[str, Any]] | None = None,
    deadline: datetime | None = None,
    relay_session_id: uuid.UUID | None = None,
) -> BackgroundRun:
    run = BackgroundRun(
        bot_id=bot.id,
        session_key="oc_chat",
        relay_session_id=relay_session_id or uuid.uuid4(),
        origin_task_id=1,
        owner_platform_user_id=OWNER,
        node_id=relay.runtime_node_id,
        relay_server_id=relay.id,
        platform="feishu",
        chat_id="oc_chat",
        chat_type="group",
        origin_message_id="om_origin",
        tasks=tasks_ if tasks_ is not None else [bg_task("a"), bg_task("b")],
        deadline=deadline or datetime.now(UTC) + timedelta(hours=1),
        card_ref={"task_id": 1},
    )
    session.add(run)
    await session.commit()
    return run


async def system_task(session: AsyncSession, bot: Bot, kind: str, payload: dict[str, Any]) -> Task:
    t = await tasks.enqueue(session, NewTask(bot_id=bot.id, kind=kind, payload=payload))
    await session.commit()
    assert t is not None
    return await claim_task(session, t.id, lane=t.lane)


async def with_delivery(session: AsyncSession, task: Task, message_id: str) -> None:
    """给任务建流与飞书投递记录（卡片按钮按投递记录里的消息 ID 核对）。"""
    from coreman.core.db.models import FeishuDelivery

    await streams.create(
        session,
        task_id=task.id,
        bot_id=task.bot_id,
        platform="feishu",
        stream_id=f"s-{task.id}",
        reply_context={},
        lease_generation=1,
        running_since=datetime.now(UTC),
    )
    session.add(
        FeishuDelivery(task_id=task.id, bot_id=task.bot_id, card_id="c1", message_id=message_id)
    )
    await session.commit()


# ---- 进入后台阶段 -----------------------------------------------------------------


async def test_main_turn_with_background_tasks_enters_the_background_phase(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await feishu_bot(db_session)
    relay = LiveRelay(chats=[main_turn_with_background()])
    t = await feishu_task(db_session, bot, "帮我开发两个功能")
    await run_chat(db_engine, t, relay)

    [body] = relay.bodies("/v1/chat/completions")
    assert body["live_owner"] == OWNER and body["live_deadline"] > datetime.now(UTC).timestamp()
    assert "live_join" not in body
    assert BACKGROUND_TASKS_RULE in body["messages"][0]["content"]

    run = (await db_session.execute(select(BackgroundRun))).scalar_one()
    assert run.state == "running" and run.origin_task_id == t.id
    assert run.owner_platform_user_id == OWNER and [x["id"] for x in run.tasks] == ["a", "b"]
    assert int(run.deadline.timestamp()) == DEADLINE
    assert run.card_ref == {"task_id": t.id}
    # 本轮签发的令牌记在这段后台阶段上，后台续出的内容按它过滤。
    token = body["env_vars"]["BOT_TOKEN_ERP"]
    ctx = build_ctx(db_engine, t)
    assert token in background_runs.open_secrets(ctx.cipher, run)

    stream = (await db_session.execute(select(TaskStream))).scalar_one()
    assert stream.final_text.endswith(msg("background_started_suffix", n=2))
    assert stream.activity["background"]["run_id"] == run.id
    assert (await tasks.get(db_session, t.id)).status == "succeeded"
    watch = (
        await db_session.execute(select(Task).where(Task.kind == background_runs.WATCH_KIND))
    ).scalar_one()
    assert watch.payload == {"run_id": run.id, "generation": 0} and watch.session_key is None
    # 没做完不算完成：不发长任务完成提醒。
    assert [i for i in await outbox_items(db_session) if "_long_done" in i.payload] == []


async def test_node_without_the_capability_keeps_todays_behaviour(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await feishu_bot(db_session, capable=False)
    relay = LiveRelay(chats=[plain_turn()])
    t = await feishu_task(db_session, bot, "你好")
    await run_chat(db_engine, t, relay)
    [body] = relay.bodies("/v1/chat/completions")
    assert "live_owner" not in body
    assert BACKGROUND_TASKS_RULE not in body["messages"][0]["content"]


async def test_wecom_bots_never_enter_the_background_phase(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await feishu_bot(db_session)
    bot.platform = "wecom"
    await db_session.commit()
    relay = LiveRelay(chats=[plain_turn()])
    t = await feishu_task(db_session, bot, "你好")
    await run_chat(db_engine, t, relay)
    [body] = relay.bodies("/v1/chat/completions")
    assert "live_owner" not in body
    assert BACKGROUND_TASKS_RULE not in body["messages"][0]["content"]


# ---- 本人接着发 ------------------------------------------------------------------


async def test_owner_message_joins_the_live_process_without_issuing_tokens(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, relay_row = await feishu_bot(db_session)
    first = LiveRelay(chats=[main_turn_with_background()])
    await run_chat(db_engine, await feishu_task(db_session, bot, "开工"), first)
    issued = await db_session.scalar(select(func.count()).select_from(BusinessTokenIssue))
    assert issued
    joined = LiveRelay(
        chats=[[runtime({"live_joined": {"tasks": [bg_task("a")]}}), *plain_turn("还在跑")]]
    )
    t = await feishu_task(db_session, bot, "现在什么进展")
    await run_chat(db_engine, t, joined)

    [body] = joined.bodies("/v1/chat/completions")
    assert body["live_join"] is True and body["live_owner"] == OWNER
    assert "BOT_TOKEN_ERP" not in body.get("env_vars", {})
    assert await db_session.scalar(select(func.count()).select_from(BusinessTokenIssue)) == issued
    stream = await streams.get(db_session, t.id)
    assert stream is not None and msg("live_joined_line", n=1) in stream.thinking_md
    # 后台还有一项在跑：不标「回复已生成」，写明还在跑、做完回复在哪条消息下。
    assert stream.final_text.endswith(msg("live_running_suffix", n=1, origin="开工"))
    assert msg("done_suffix").strip() not in stream.final_text
    current = await tasks.get(db_session, t.id)
    assert current is not None and current.payload["live_join"]
    # 接进去的一轮不新建后台阶段。
    assert await db_session.scalar(select(func.count()).select_from(BackgroundRun)) == 1


async def test_joined_reply_without_text_still_says_background_is_running(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await feishu_bot(db_session)
    await run_chat(
        db_engine,
        await feishu_task(db_session, bot, "@_user_1 帮我合并 361，等检查通过"),
        LiveRelay(chats=[main_turn_with_background()]),
    )
    joined = LiveRelay(
        chats=[
            [
                runtime({"live_joined": {"tasks": [bg_task("a"), bg_task("b")]}}),
                FINISH,
                _chunk(None, usage=USAGE),
                DONE,
            ]
        ]
    )
    t = await feishu_task(db_session, bot, "先只合并 361")
    await run_chat(db_engine, t, joined)
    stream = await streams.get(db_session, t.id)
    assert stream is not None
    # 原消息按开头一句引用（去掉 @ 提及）。
    assert stream.final_text.endswith(
        msg("live_running_suffix", n=2, origin="帮我合并 361，等检查通过")
    )
    assert msg("done_suffix").strip() not in stream.final_text


async def test_joined_reply_is_marked_done_when_nothing_is_left_running(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await feishu_bot(db_session)
    await run_chat(
        db_engine,
        await feishu_task(db_session, bot, "开工"),
        LiveRelay(chats=[main_turn_with_background()]),
    )
    joined = LiveRelay(
        chats=[
            [
                runtime({"live_joined": {"tasks": [bg_task("a")]}}),
                runtime({"background_tasks": [bg_task("a", "completed")]}),
                *plain_turn("A 做完了"),
            ]
        ]
    )
    t = await feishu_task(db_session, bot, "进展？")
    await run_chat(db_engine, t, joined)
    stream = await streams.get(db_session, t.id)
    assert stream is not None
    # 后台任务在这一轮里全部完成：照旧并入说明 + ✅。
    assert stream.final_text.endswith(msg("live_merged_note", which="①") + msg("done_suffix"))
    assert "⏳" not in stream.final_text


async def test_turn_without_background_tasks_is_still_marked_done(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await feishu_bot(db_session)
    t = await feishu_task(db_session, bot, "你好")
    await run_chat(db_engine, t, LiveRelay(chats=[plain_turn()]))
    stream = await streams.get(db_session, t.id)
    assert stream is not None and stream.final_text.endswith(msg("done_suffix"))
    assert "⏳" not in stream.final_text


async def test_joined_turn_falls_back_to_a_normal_turn_when_the_session_just_ended(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, relay_row = await feishu_bot(db_session)
    first = LiveRelay(chats=[main_turn_with_background()])
    await run_chat(db_engine, await feishu_task(db_session, bot, "开工"), first)
    old = (await db_session.execute(select(BackgroundRun))).scalar_one()
    relay = LiveRelay(chats=[(409, "not_live"), main_turn_with_background()])
    t = await feishu_task(db_session, bot, "接着做")
    await run_chat(db_engine, t, relay)
    joined, normal = relay.bodies("/v1/chat/completions")
    assert joined["live_join"] is True and "BOT_TOKEN_ERP" not in joined.get("env_vars", {})
    assert "live_join" not in normal and normal["env_vars"]["BOT_TOKEN_ERP"]
    # 新进程照样带 owner 与到期时间，可以自己再进后台阶段。
    assert normal["live_owner"] == OWNER and normal["live_deadline"] > time.time()
    stream = await streams.get(db_session, t.id)
    assert stream is not None and stream.final_text.endswith(msg("background_started_suffix", n=2))
    assert (await tasks.get(db_session, t.id)).status == "succeeded"
    await db_session.refresh(old)
    assert old.state == "finished" and old.close_reason == background_runs.REPLACED
    runs = list((await db_session.execute(select(BackgroundRun))).scalars())
    assert [r.state for r in runs if r.id != old.id] == ["running"]


async def test_owner_turns_that_cannot_join_wait_instead_of_going_out_without_owner(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """本人发起、但回复交给平台解析的一轮不能写进后台进程：排队等，不发不带 owner 的请求。"""

    class Structured(ChatTaskHandler):
        def _structured_reply(self, ctx):  # type: ignore[no-untyped-def]
            return True

    bot, relay_row = await feishu_bot(db_session)
    run = await make_run(db_session, bot, relay_row)
    t = await feishu_task(db_session, bot, "本人但不能接")
    relay = LiveRelay(chats=[plain_turn()])
    await Structured().run(build_ctx(db_engine, t, relay_client_factory=relay.client))
    assert relay.requests == []
    held = await tasks.get(db_session, t.id)
    assert held is not None and held.status == "queued"
    assert held.payload["background_hold"] == str(run.id)


async def test_hold_near_the_deadline_backs_off_and_an_overdue_run_is_concluded(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, relay_row = await feishu_bot(db_session)
    # 到期 400 秒：放行时刻（到期 + 300 秒）已过，但还没到平台收尾的时候。
    run = await make_run(
        db_session, bot, relay_row, deadline=datetime.now(UTC) - timedelta(seconds=400)
    )
    t = await feishu_task(db_session, bot, "我也有事", sender="ou_other", chat_type="group")
    await run_chat(db_engine, t, LiveRelay(chats=[plain_turn()]))
    held = await tasks.get(db_session, t.id)
    assert held is not None and held.status == "queued"
    assert held.run_after >= datetime.now(UTC) + timedelta(seconds=10)
    # 到期 700 秒还挂着：就地按到期收尾，这一轮照常处理。
    run.deadline = datetime.now(UTC) - timedelta(seconds=700)
    held.run_after = datetime.now(UTC)
    await db_session.commit()
    relay = LiveRelay(chats=[plain_turn("轮到我了")])
    await run_chat(db_engine, await claim_task(db_session, t.id), relay)
    await db_session.refresh(run)
    assert run.state == "expired"
    assert len(relay.bodies("/v1/chat/completions")) == 1


async def test_another_sender_waits_for_the_background_phase(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, relay_row = await feishu_bot(db_session)
    run = await make_run(db_session, bot, relay_row)
    relay = LiveRelay(chats=[plain_turn()])
    t = await feishu_task(db_session, bot, "我也有事", sender="ou_other", chat_type="group")
    await run_chat(db_engine, t, relay)
    assert relay.requests == []
    held = await tasks.get(db_session, t.id)
    assert held is not None and held.status == "queued"
    assert held.payload["background_hold"] == str(run.id)
    assert held.run_after >= run.deadline
    # 后台阶段结束：立刻放行。
    locked = await db_session.get(BackgroundRun, run.id, with_for_update=True)
    assert locked is not None and await background_runs.end(db_session, locked, "finished")
    await db_session.commit()
    released = await tasks.get(db_session, t.id)
    assert released is not None and "background_hold" not in released.payload
    assert released.run_after <= datetime.now(UTC)


async def test_gateway_holds_other_senders_and_offers_the_background_choice(
    db_session: AsyncSession,
) -> None:
    from coreman.core.chat import busy_choice
    from coreman.core.wecom.messages import InboundMessage, Sender
    from coreman.runtime.gateway_common.inbound import InboundBot, enqueue_inbound

    bot, relay_row = await feishu_bot(db_session)
    run = await make_run(db_session, bot, relay_row)

    def message(sender: str, n: int) -> InboundMessage:
        return InboundMessage(
            platform="feishu",
            bot_id=bot.id,
            kind="message",
            chat_type="group",
            chat_id="oc_chat",
            sender=Sender(platform_user_id=sender),
            message_id=f"om_{sender}_{n}",
            parts=[{"type": "text", "text": "看一下"}],
            mentions_bot=True,
            reply_context={"chat_id": "oc_chat", "message_id": f"om_{sender}_{n}"},
        )

    inbound_bot: InboundBot = SimpleNamespace(  # type: ignore[assignment]
        id=bot.id, bot_key=bot.bot_key, welcome_message=None
    )
    other = await enqueue_inbound(
        db_session, inbound_bot, message("ou_other", 1), lease_generation=1
    )
    again = await enqueue_inbound(
        db_session, inbound_bot, message("ou_other", 2), lease_generation=1
    )
    mine = await enqueue_inbound(db_session, inbound_bot, message(OWNER, 3), lease_generation=1)
    await db_session.commit()
    assert other is not None and again is not None and mine is not None
    assert other.payload["background_hold"] == str(run.id)
    assert other.payload["serialize_session"] is True
    assert "background_hold" not in mine.payload and not mine.payload.get("serialize_session")
    cards = [i for i in await outbox_items(db_session) if "card" in i.payload]
    # 同一个人连发两条只发一张卡。
    assert len(cards) == 1
    assert cards[0].payload["card"] == busy_choice.background_offer_card(
        "feishu", other.id, running=2
    )


# ---- 观察任务 ------------------------------------------------------------------


async def test_watch_posts_completed_turns_skips_merged_ones_and_finishes(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, relay_row = await feishu_bot(db_session)
    run = await make_run(db_session, bot, relay_row)
    attach = [
        runtime({"background_tasks": [bg_task("a"), bg_task("b")]}),
        runtime({"background_tasks": [bg_task("a", "completed"), bg_task("b")]}),
        _text("A 做完了"),
        _chunk(None, usage=USAGE),
        runtime({"turn_end": {"merged": False}}),
        runtime({"background_tasks": [bg_task("b", "completed")]}),
        runtime({"turn_end": {"merged": True}}),
        runtime({"background_end": {"state": "finished", "tasks": [bg_task("b", "completed")]}}),
        DONE,
    ]
    relay = LiveRelay(attaches=[attach])
    t = await system_task(db_session, bot, background_runs.WATCH_KIND, {"run_id": run.id})
    ctx = build_ctx(db_engine, t, relay_client_factory=relay.client)
    await BackgroundWatchHandler().run(ctx)

    assert relay.bodies("/live/attach") == [{"session_id": str(run.relay_session_id)}]
    await db_session.refresh(run)
    assert run.state == "finished" and run.turns == 1
    assert [x["status"] for x in run.tasks] == ["completed", "completed"]
    items = await outbox_items(db_session)
    replies = [i for i in items if "markdown" in i.payload]
    assert len(replies) == 1
    [reply] = replies
    assert reply.payload["markdown"].startswith(msg("background_done_prefix", which="①"))
    assert "A 做完了" in reply.payload["markdown"]
    assert reply.payload["_mention_user_id"] == OWNER
    assert reply.target == {"chat_id": "oc_chat", "message_id": "om_origin"}
    cards = [i for i in items if "_background_card" in i.payload]
    assert cards and cards[-1].payload["_background_card"]["state"] == "finished"
    log = (await db_session.execute(select(ChatLog))).scalar_one()
    assert log.task_id == t.id and log.output_tokens == USAGE["completion_tokens"]
    assert (await tasks.get(db_session, t.id)).status == "succeeded"


async def test_watch_reports_unfinished_tasks_when_stopped(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, relay_row = await feishu_bot(db_session)
    run = await make_run(db_session, bot, relay_row)
    locked = await db_session.get(BackgroundRun, run.id, with_for_update=True)
    assert locked is not None and await background_runs.request_close(db_session, locked, "user")
    await db_session.commit()
    attach = [
        runtime({"background_end": {"state": "stopped", "tasks": [bg_task("a", "completed")]}}),
        DONE,
    ]
    relay = LiveRelay(attaches=[attach])
    t = await system_task(db_session, bot, background_runs.WATCH_KIND, {"run_id": run.id})
    await BackgroundWatchHandler().run(build_ctx(db_engine, t, relay_client_factory=relay.client))
    await db_session.refresh(run)
    assert run.state == "stopped"
    [notice] = [i for i in await outbox_items(db_session) if i.dedupe_key == f"bg-end:{run.id}"]
    text = notice.payload["markdown"]
    assert text.startswith("⏹ 后台任务已停止") and "任务b" in text and "任务a" not in text


async def test_watch_marks_the_run_lost_when_the_node_no_longer_has_it(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, relay_row = await feishu_bot(db_session)
    run = await make_run(db_session, bot, relay_row)
    node = await db_session.get(RuntimeNode, relay_row.runtime_node_id)
    assert node is not None
    node.live_sessions = []
    await db_session.commit()
    relay = LiveRelay(attaches=[(404, "not_live")])
    t = await system_task(db_session, bot, background_runs.WATCH_KIND, {"run_id": run.id})
    await BackgroundWatchHandler(reconnect=(0.01,)).run(
        build_ctx(db_engine, t, relay_client_factory=relay.client)
    )
    await db_session.refresh(run)
    assert run.state == "lost"
    [notice] = [i for i in await outbox_items(db_session) if i.dedupe_key == f"bg-end:{run.id}"]
    assert notice.payload["markdown"].startswith("❌ 后台任务因运行节点中断而停止")


async def test_watch_reconnects_after_the_stream_drops(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, relay_row = await feishu_bot(db_session)
    run = await make_run(db_session, bot, relay_row)
    relay = LiveRelay(
        attaches=[
            [runtime({"background_tasks": [bg_task("a")]})],  # 没有 [DONE] 就断了
            [runtime({"background_end": {"state": "expired", "tasks": []}}), DONE],
        ]
    )
    t = await system_task(db_session, bot, background_runs.WATCH_KIND, {"run_id": run.id})
    await BackgroundWatchHandler(reconnect=(0.01,)).run(
        build_ctx(db_engine, t, relay_client_factory=relay.client)
    )
    assert len(relay.bodies("/live/attach")) == 2
    await db_session.refresh(run)
    assert run.state == "expired"


# ---- 停止、重置、看护 -----------------------------------------------------------


async def test_control_task_calls_the_driver(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, relay_row = await feishu_bot(db_session)
    run = await make_run(db_session, bot, relay_row)
    relay = LiveRelay()
    payload = {"run_id": run.id, "action": "stop_task", "task_id": "a"}
    t = await system_task(db_session, bot, background_runs.CONTROL_KIND, payload)
    await BackgroundControlHandler().run(build_ctx(db_engine, t, relay_client_factory=relay.client))
    assert relay.requests == [
        ("/live/stop_task", {"session_id": str(run.relay_session_id), "task_id": "a"})
    ]
    assert (await tasks.get(db_session, t.id)).result == {"outcome": "done"}


async def test_card_stop_buttons_are_for_the_owner_and_bot_admins(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    from coreman.core.db.models import BotMember

    bot, relay_row = await feishu_bot(db_session)
    run = await make_run(db_session, bot, relay_row)
    origin = await feishu_task(db_session, bot, "开工", claim=False)
    await with_delivery(db_session, origin, "om_card")
    run.card_ref = {"task_id": origin.id}
    admin = User(login_name="admin", display_name="管理员")
    db_session.add(admin)
    await db_session.flush()
    db_session.add_all(
        [
            BotMember(bot_id=bot.id, user_id=admin.id),
            UserIdentity(user_id=admin.id, platform="feishu", platform_user_id="ou_admin"),
        ]
    )
    await db_session.commit()

    async def click(sender: str, level: str, message_id: str = "om_card") -> str:
        t = await system_task(db_session, bot, "card_action", {"platform_user_id": sender})
        ctx = build_ctx(db_engine, t)
        inbound = InboundEvent(reply_context={"message_id": message_id})
        action = {"task_id": background_runs.card_task_id(run.id), "level": level}
        async with ctx.session_factory() as session:
            result = await handle_card(session, ctx, bot, inbound, action)
            await session.commit()
        await tasks.finish(db_session, t.id, status="succeeded")
        await db_session.commit()
        return result

    assert await click("ou_stranger", "a") == "denied"
    assert await click(OWNER, "a", message_id="om_other") == "ignored"
    assert await click(OWNER, "a") == "stopping"
    assert await click("ou_admin", background_runs.ALL) == "stopping"
    controls = list(
        (
            await db_session.execute(
                select(Task).where(Task.kind == background_runs.CONTROL_KIND).order_by(Task.id)
            )
        ).scalars()
    )
    assert [c.payload["action"] for c in controls] == ["stop_task", "close"]
    await db_session.refresh(run)
    assert run.close_reason == "user"


async def test_reset_closes_the_background_phase_first(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    from coreman.runtime.worker.commands import do_reset

    bot, relay_row = await feishu_bot(db_session)
    run = await make_run(db_session, bot, relay_row)
    t = await system_task(db_session, bot, "command", {})
    ctx = build_ctx(db_engine, t)
    async with ctx.session_factory() as session:
        assert await do_reset(session, ctx, bot.id, "oc_chat") == msg("session_reset_ok")
        await session.commit()
    await db_session.refresh(run)
    assert run.state == "stopped" and run.close_reason == "reset"
    control = (
        await db_session.execute(select(Task).where(Task.kind == background_runs.CONTROL_KIND))
    ).scalar_one()
    assert control.payload == {"run_id": run.id, "action": "close", "reason": "reset"}


async def test_clear_bot_paths_close_every_running_phase(db_session: AsyncSession) -> None:
    bot, relay_row = await feishu_bot(db_session)
    run = await make_run(db_session, bot, relay_row)
    assert await background_runs.close_for_bot(db_session, bot.id) == 1
    await db_session.commit()
    await db_session.refresh(run)
    assert run.state == "stopped"


async def test_runtime_switch_tells_the_asker_that_background_work_was_stopped(
    db_session: AsyncSession,
) -> None:
    bot, relay_row = await feishu_bot(db_session)
    run = await make_run(db_session, bot, relay_row)
    assert any(t.get("status") == "running" for t in run.tasks or [])
    closed = await background_runs.close_for_bot(
        db_session, bot.id, notice="background_switched_notice"
    )
    await db_session.commit()
    await db_session.refresh(run)
    assert closed == 1 and run.state == "stopped"
    assert all(t.get("status") != "running" for t in run.tasks or [])
    notice = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key == f"bg-end:{run.id}")
    )
    assert notice is not None
    prefix = msg("background_switched_notice", elapsed="", lines="").split("（")[0]
    assert notice.payload["markdown"].startswith(prefix)
    assert "任务a" in notice.payload["markdown"]
    card = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key == f"bg-card:{run.id}:switch")
    )
    assert card is not None


async def test_tick_expires_due_runs_and_marks_offline_nodes_lost(db_session: AsyncSession) -> None:
    bot, relay_row = await feishu_bot(db_session)
    due = await make_run(
        db_session, bot, relay_row, deadline=datetime.now(UTC) - timedelta(minutes=1)
    )
    now = datetime.now(UTC)
    counts = await background_runs.tick(db_session, now)
    await db_session.commit()
    assert counts["background_expired"] == 1
    await db_session.refresh(due)
    assert due.close_reason == "expired" and due.state == "running"
    # 观察任务没了也补一个。
    assert counts["background_rewatched"] == 1
    # close 发出去 3 分钟还没回音：平台按到期收尾并告知。
    counts = await background_runs.tick(db_session, now + timedelta(minutes=4))
    await db_session.commit()
    await db_session.refresh(due)
    assert due.state == "expired" and counts["background_concluded"] == 1
    assert [i for i in await outbox_items(db_session) if i.dedupe_key == f"bg-end:{due.id}"]

    other_bot, other_relay = bot, relay_row
    lost = BackgroundRun(
        bot_id=other_bot.id,
        session_key="oc_other",
        relay_session_id=uuid.uuid4(),
        origin_task_id=2,
        owner_platform_user_id=OWNER,
        node_id=other_relay.runtime_node_id,
        relay_server_id=other_relay.id,
        platform="feishu",
        chat_id="oc_other",
        chat_type="single",
        deadline=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(lost)
    node = await db_session.get(RuntimeNode, other_relay.runtime_node_id)
    assert node is not None
    node.heartbeat_at = datetime.now(UTC) - timedelta(minutes=5)
    await db_session.commit()
    counts = await background_runs.tick(db_session, datetime.now(UTC))
    await db_session.commit()
    await db_session.refresh(lost)
    assert lost.state == "lost"


async def test_busy_choice_stop_closes_the_background_phase(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    from coreman.core.chat import busy_choice
    from coreman.runtime.worker.busy_choice import handle

    bot, relay_row = await feishu_bot(db_session)
    run = await make_run(db_session, bot, relay_row)
    held = await feishu_task(
        db_session, bot, "我的事", sender="ou_other", chat_type="group", claim=False
    )
    held.payload = {**held.payload, "background_hold": str(run.id), "chat_id": "oc_chat"}
    held.run_after = background_runs.hold_until(run)
    await db_session.commit()
    click = await system_task(
        db_session, bot, "card_action", {"platform_user_id": "ou_other", "chat_id": "oc_chat"}
    )
    ctx = build_ctx(db_engine, click)
    action = {"task_id": busy_choice.card_task_id(held.id), "event_key": busy_choice.BG_STOP}
    async with ctx.session_factory() as session:
        result, card = await handle(session, ctx, bot, action, "")
        await session.commit()
    assert result == "stop" and card is not None
    await db_session.refresh(run)
    assert run.close_reason == "user"


async def test_stop_during_a_joined_turn_asks_for_confirmation(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    from coreman.core.chat import stream_stop
    from coreman.runtime.worker.stream_stop import handle

    bot, relay_row = await feishu_bot(db_session)
    run = await make_run(db_session, bot, relay_row)
    target = await feishu_task(db_session, bot, "进展如何", chat_type="group")
    target.payload = {**target.payload, "live_join": run.id}
    await db_session.commit()
    await with_delivery(db_session, target, "om_turn")

    async def click(message_id: str, level: str = "") -> str:
        t = await system_task(db_session, bot, "card_action", {"platform_user_id": OWNER})
        ctx = build_ctx(db_engine, t)
        inbound = InboundEvent(chat_id="oc_chat", reply_context={"message_id": message_id})
        action = {"task_id": stream_stop.card_task_id(target.id), "level": level}
        async with ctx.session_factory() as session:
            result = await handle(session, ctx, bot, inbound, action)
            await session.commit()
        await tasks.finish(db_session, t.id, status="succeeded")
        await db_session.commit()
        return result

    assert await click("om_turn") == "confirm"
    [card] = [i for i in await outbox_items(db_session) if "_live_stop_confirm" in i.payload]
    assert msg("live_stop_confirm_desc", n=2) in json.dumps(card.payload, ensure_ascii=False)
    # 确认卡还没发出去：伪造的确认点击不算。
    assert await click("om_confirm", stream_stop.CONFIRMED) == "ignored"
    card.status = "sent"
    card.payload = {**card.payload, "_feishu_message_id": "om_confirm"}
    await db_session.commit()
    assert await click("om_confirm", stream_stop.CONFIRMED) == "stopped"
    current = await tasks.get(db_session, target.id)
    assert current is not None and current.cancel_reason == "user_stop"


async def test_joined_reply_says_which_background_results_it_includes(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await feishu_bot(db_session)
    await run_chat(
        db_engine,
        await feishu_task(db_session, bot, "开工"),
        LiveRelay(chats=[main_turn_with_background()]),
    )
    joined = LiveRelay(
        chats=[
            [
                runtime({"live_joined": {"tasks": [bg_task("a"), bg_task("b")]}}),
                runtime({"background_tasks": [bg_task("a", "completed"), bg_task("b")]}),
                *plain_turn("A 也做完了"),
            ]
        ]
    )
    t = await feishu_task(db_session, bot, "进展？")
    await run_chat(db_engine, t, joined)
    stream = await streams.get(db_session, t.id)
    assert stream is not None
    # ① 在这一轮里完成、② 还在跑：写并入说明，但不标「回复已生成」。
    assert stream.final_text.endswith(
        msg("live_merged_note", which="①") + msg("live_running_suffix", n=1, origin="开工")
    )
    assert msg("done_suffix").strip() not in stream.final_text


async def test_watch_delivers_partial_and_merged_text_once_across_replays(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """本人接进来时已经写出的半段照发；重连后驱动补发的同一回合不重复发。"""
    bot, relay_row = await feishu_bot(db_session)
    run = await make_run(db_session, bot, relay_row)
    first = [
        _text("开头半段"),
        runtime({"turn_end": {"merged": False, "partial": True}}),
        _text("并入但有内容"),
        runtime({"turn_end": {"merged": True}}),
    ]  # 没有 [DONE]：断了，重连后补发
    replay = [
        _text("开头半段"),
        runtime({"turn_end": {"merged": False, "partial": True}}),
        runtime({"background_end": {"state": "finished", "tasks": []}}),
        DONE,
    ]
    relay = LiveRelay(attaches=[first, replay])
    t = await system_task(db_session, bot, background_runs.WATCH_KIND, {"run_id": run.id})
    await BackgroundWatchHandler(reconnect=(0.01,)).run(
        build_ctx(db_engine, t, relay_client_factory=relay.client)
    )
    replies = [
        i.payload["markdown"] for i in await outbox_items(db_session) if "markdown" in i.payload
    ]
    assert len(replies) == 2
    assert "开头半段" in replies[0] and "并入但有内容" in replies[1]
    await db_session.refresh(run)
    assert run.state == "finished" and run.turns == 2


async def test_watch_records_usage_before_handing_over_on_drain(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    from coreman.runtime.worker.background_watch import DRAIN_REASON

    bot, relay_row = await feishu_bot(db_session)
    run = await make_run(db_session, bot, relay_row)
    relay = LiveRelay(
        attaches=[[_text("A 完成"), _chunk(None, usage=USAGE), runtime({"turn_end": {}})]]
    )
    t = await system_task(db_session, bot, background_runs.WATCH_KIND, {"run_id": run.id})
    ctx = build_ctx(db_engine, t, relay_client_factory=relay.client)
    running = asyncio.create_task(BackgroundWatchHandler(reconnect=(30.0,)).run(ctx))
    await asyncio.sleep(1.0)
    ctx.request_cancel(DRAIN_REASON)
    await asyncio.wait_for(running, 10)
    log = (await db_session.execute(select(ChatLog))).scalar_one()
    assert log.task_id == t.id and log.output_tokens == USAGE["completion_tokens"]
    nxt = (
        await db_session.execute(
            select(Task).where(Task.kind == background_runs.WATCH_KIND, Task.id != t.id)
        )
    ).scalar_one()
    assert nxt.payload == {"run_id": run.id, "generation": 1}


async def test_live_busy_is_retried_as_a_joined_turn(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await feishu_bot(db_session)
    await run_chat(
        db_engine,
        await feishu_task(db_session, bot, "开工"),
        LiveRelay(chats=[main_turn_with_background()]),
    )
    relay = LiveRelay(
        chats=[
            (409, "live_busy"),
            [runtime({"live_joined": {"tasks": [bg_task("a")]}}), *plain_turn("好了")],
        ]
    )
    t = await feishu_task(db_session, bot, "进展？")
    await run_chat(db_engine, t, relay)
    first, second = relay.bodies("/v1/chat/completions")
    assert first["live_join"] is True and second["live_join"] is True
    assert (await tasks.get(db_session, t.id)).status == "succeeded"


async def test_usage_only_turn_end_posts_nothing(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, relay_row = await feishu_bot(db_session)
    run = await make_run(db_session, bot, relay_row)
    attach = [
        runtime({"background_tasks": [bg_task("a", "completed"), bg_task("b")]}),
        _chunk(None, usage=USAGE),
        runtime({"turn_end": {"merged": False}}),
        runtime({"background_end": {"state": "finished", "tasks": []}}),
        DONE,
    ]
    relay = LiveRelay(attaches=[attach])
    t = await system_task(db_session, bot, background_runs.WATCH_KIND, {"run_id": run.id})
    await BackgroundWatchHandler().run(build_ctx(db_engine, t, relay_client_factory=relay.client))
    assert [i for i in await outbox_items(db_session) if "markdown" in i.payload] == []
    log = (await db_session.execute(select(ChatLog))).scalar_one()
    assert log.output_tokens == USAGE["completion_tokens"]

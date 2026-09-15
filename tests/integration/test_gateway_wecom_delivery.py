"""企微出站投递的边界：连接不可用时不认领、同步拒绝的分流、只有流失效码才封流、内存状态回收。

复用 `test_gateway_wecom.py` 的夹具函数，保证两边跑的是同一套网关配置。
"""

import asyncio
import json
import time
from datetime import UTC, datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from websockets.exceptions import ConnectionClosedError

from coreman.core.bots.secrets import CREDENTIALS_AAD
from coreman.core.bus import outbox, streams
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, OutboxItem
from coreman.core.db.session import make_session_factory
from coreman.core.i18n.messages import msg
from coreman.runtime.gateway_wecom.pusher import RETIRE_SECONDS
from tests.fakes.fake_wecom_ws import FakeWeComWs
from tests.integration.test_gateway_wecom import _bot, _seed_stream, _start, _stream_row, _wait
from tests.integration.worker_helpers import MASTER


async def _outbox_rows(db_engine: AsyncEngine) -> dict[str, OutboxItem]:
    async with make_session_factory(db_engine)() as s:
        rows = (await s.execute(select(OutboxItem).order_by(OutboxItem.id))).scalars()
        return {r.dedupe_key: r for r in rows}


async def _add_sends(db_engine: AsyncEngine, bot_id, items) -> None:  # type: ignore[no-untyped-def]
    async with make_session_factory(db_engine)() as s:
        for key, payload in items:
            await outbox.add(
                s,
                bot_id=bot_id,
                platform="wecom",
                kind="send",
                dedupe_key=key,
                target={"chat_id": "zs"},
                payload=payload,
            )
        await s.commit()


async def _make_due(db_engine: AsyncEngine) -> None:
    async with make_session_factory(db_engine)() as s:
        await s.execute(
            update(OutboxItem).values(not_before=func.now() - text("interval '1 second'"))
        )
        await s.commit()


async def test_a_stuck_bot_does_not_hold_up_the_other_bots(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    """出站按 bot 各跑各的：一个 bot 卡在等回执，同实例的其它 bot 照常发，也不叠第二趟。"""
    bot = await _bot(db_session)
    other = Bot(
        bot_key="other_bot",
        platform="wecom",
        name="另一个",
        created_by=bot.created_by,
        model="vllm/claude-sonnet-4-6",
        working_dir="/d",
        credentials_enc=Cipher(MASTER).encrypt(
            json.dumps({"bot_id": "bot2", "secret": "sec2"}), CREDENTIALS_AAD
        ),
    )
    db_session.add(other)
    await db_session.commit()
    fake = FakeWeComWs(accepted={"bot1": "sec", "bot2": "sec2"})
    url = await fake.start()
    service, runner = await _start(url)
    stuck = asyncio.Event()
    try:
        await _wait(lambda: len(service.runners) == 2 and {"bot1", "bot2"} <= set(fake.connections))
        calls: list[int] = []

        async def hang() -> None:
            calls.append(1)
            await stuck.wait()

        service.runners[bot.id].outbox.consume = hang  # type: ignore[method-assign]
        await _add_sends(db_engine, bot.id, [("51:send:0", {"markdown": "卡住的"})])
        await _wait(lambda: bool(calls))
        await _add_sends(db_engine, other.id, [("52:send:0", {"markdown": "照常"})])

        async def other_sent() -> bool:
            row = (await _outbox_rows(db_engine)).get("52:send:0")
            return row is not None and row.status == "sent"

        await _wait(other_sent)
        await asyncio.sleep(0.5)  # 再过几轮兜底轮询
        assert calls == [1]  # 卡住的那一趟没跑完，不会再叠一趟
        assert [b["markdown"]["content"] for b in fake.sent_messages()] == ["照常"]
    finally:
        stuck.set()
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()


async def test_outbox_queued_while_the_subscription_is_rejected_is_delivered_later(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    """订阅被拒 / 刚认领还在建连时入队的出站：一条都不领，连上之后按顺序送达，不级联失败。

    以前这段窗口里每一轮兜底轮询都会领一条、写帧失败、判「结果未知」终态失败，再把同组后续
    项全部级联判死——网关每次重启、每次重连都静默丢一批后台推送。
    """
    bot = await _bot(db_session)
    fake = FakeWeComWs(accepted={})  # 先拒掉订阅：连接停在 auth_failed 的退避里
    url = await fake.start()
    service, runner = await _start(url)
    try:
        await _wait(lambda: bool(service.runners) and fake.subscribe_attempts >= 1)
        await _add_sends(
            db_engine,
            bot.id,
            [
                ("41:send:1", {"markdown": "第一段"}),
                ("41:send:final", {"markdown": "终稿"}),
                ("41:card:0", {"card": {"card_type": "text_notice"}}),
            ],
        )
        await asyncio.sleep(1.0)  # 让兜底轮询空转好几轮
        rows = await _outbox_rows(db_engine)
        assert {k: (r.status, r.attempts) for k, r in rows.items()} == {
            "41:send:1": ("pending", 0),
            "41:send:final": ("pending", 0),
            "41:card:0": ("pending", 0),
        }
        assert fake.sent_messages() == []

        fake.accepted["bot1"] = "sec"  # 凭证恢复：下一次重连订阅成功

        async def all_sent() -> bool:
            return all(r.status == "sent" for r in (await _outbox_rows(db_engine)).values())

        await _wait(all_sent, timeout=12)
        rows = await _outbox_rows(db_engine)
        assert all(r.attempts == 0 for r in rows.values())
        bodies = fake.sent_messages()
        assert [b["msgtype"] for b in bodies] == ["markdown", "markdown", "template_card"]
        assert [b["markdown"]["content"] for b in bodies[:2]] == ["第一段", "终稿"]
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()


async def test_a_frame_that_never_left_the_socket_is_retried_and_dependents_wait(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    """写帧那一步连接就断了：结果确定是没送到——退避重试，同组卡片等着，谁都不判死。"""
    bot = await _bot(db_session)
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    service, runner = await _start(url)
    try:
        await _wait(lambda: "bot1" in fake.connections and bool(service.runners))
        ws = service.runners[bot.id].ws
        real_send = ws.send
        calls: list[dict] = []

        async def broken_once(data):  # type: ignore[no-untyped-def]
            calls.append(data)
            if len(calls) == 1:
                raise ConnectionClosedError(None, None)
            await real_send(data)

        ws.send = broken_once  # type: ignore[method-assign]
        await _add_sends(
            db_engine,
            bot.id,
            [
                ("42:send:final", {"markdown": "终稿"}),
                ("42:card:0", {"card": {"card_type": "text_notice"}}),
            ],
        )

        async def backed_off() -> bool:
            first = (await _outbox_rows(db_engine)).get("42:send:final")
            return first is not None and first.status == "pending" and first.attempts == 1

        await _wait(backed_off)
        rows = await _outbox_rows(db_engine)
        assert rows["42:send:final"].last_error.startswith("not_sent")
        assert (rows["42:card:0"].status, rows["42:card:0"].attempts) == ("pending", 0)
        assert fake.sent_messages() == []

        await _make_due(db_engine)  # 退避到点

        async def all_sent() -> bool:
            return all(r.status == "sent" for r in (await _outbox_rows(db_engine)).values())

        await _wait(all_sent, timeout=8)
        assert [b["msgtype"] for b in fake.sent_messages()] == ["markdown", "template_card"]
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()


async def test_synchronously_rejected_proactive_finish_frame_keeps_the_offset_and_fills_the_gap(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    """主动模式那帧 finish 的回执本身就是 846606：补 `confirmed..offset`，不改写 worker 的 offset。

    以前同步拒绝只封流，下一轮「已封流」分支把行当成未完成待交接，offset 被改写成确认长度，
    而 worker 仍从旧 offset 往后推——中间那一段谁都不送。
    """
    bot = await _bot(db_session)
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    service, runner = await _start(url)
    try:
        await _wait(lambda: "bot1" in fake.connections and bool(service.runners))
        req = await fake.send_message("bot1", text="hi", msgid="m1")
        task_id = await _seed_stream(db_engine, bot.id, service, req)  # pending_text="正文"
        await _wait(lambda: bool(fake.stream_contents(req)))
        pusher = service.runners[bot.id].pusher
        # 时钟往后拨：第一帧落在错误码延迟预算之外，算它确认送达。
        clock = [time.monotonic() + 100.0]
        pusher.clock = lambda: clock[0]
        fake.reject_finish_frames(errcode=846606, errmsg="stream not exist", delay=0.0)
        full = "正文又写了一大段"
        async with make_session_factory(db_engine)() as s:
            await streams.update(
                s,
                task_id,
                pending_text=full,
                delivery_mode="proactive",
                background_state={
                    "mode": "incremental",
                    "offset": len(full),
                    "finish_suffix": msg("timeout_background_low"),
                    "switched_at": datetime.now(UTC).isoformat(),
                },
            )
            await s.commit()

        async def gap_queued() -> bool:
            return f"{task_id}:send:finish_gap" in await _outbox_rows(db_engine)

        await _wait(gap_queued)
        rows = await _outbox_rows(db_engine)
        assert list(rows) == [f"{task_id}:send:finish_gap"]
        assert rows[f"{task_id}:send:finish_gap"].payload["markdown"] == full[len("正文") :]
        row = await _stream_row(db_engine, task_id)
        assert row and row.delivery_mode == "proactive"
        assert row.background_state["offset"] == len(full)  # worker 的 offset 原封不动
        assert row.finish_pushed_at is not None

        # 收尾过了保留窗口：这条流在网关内存里的帧历史 / 封流标记全部清掉
        assert "s1" in pusher._retired
        clock[0] += RETIRE_SECONDS + 1
        await pusher.push_pending()
        assert "s1" not in pusher._retired and "s1" not in pusher._frames
        assert "s1" not in pusher.blocked and "s1" not in pusher._confirmed
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()


async def test_a_retryable_errcode_on_a_stream_frame_does_not_block_the_stream(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    """系统繁忙（-1）之类的错误码不是流失效：不封流、不转主动推送，下一轮同一个 req_id 重推。"""
    bot = await _bot(db_session)
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    service, runner = await _start(url)
    try:
        await _wait(lambda: "bot1" in fake.connections and bool(service.runners))
        fake.stream_rejections.append((-1, "system busy"))
        req = await fake.send_message("bot1", text="hi", msgid="m1")
        task_id = await _seed_stream(db_engine, bot.id, service, req)  # pending_text="正文"
        await _wait(lambda: len(fake.stream_contents(req)) >= 2, timeout=8)
        first, second = fake.stream_contents(req)[:2]
        assert first == second and not first[1]  # 同一版本原样重推

        async def pushed() -> bool:
            row = await _stream_row(db_engine, task_id)
            return bool(row and row.pushed_version == row.version)

        await _wait(pushed)
        pusher = service.runners[bot.id].pusher
        assert "s1" not in pusher.blocked
        async with make_session_factory(db_engine)() as s:
            await streams.complete(s, task_id, final_text="正文，完成")
            await s.commit()
        await _wait(lambda: fake.stream_contents(req)[-1][1])

        async def finished() -> bool:
            row = await _stream_row(db_engine, task_id)
            return bool(row and row.finish_pushed_at is not None)

        await _wait(finished)
        row = await _stream_row(db_engine, task_id)
        assert row and row.delivery_mode == "stream"
        assert await _outbox_rows(db_engine) == {}
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()

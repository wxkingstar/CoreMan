"""每应用独立 SDK 进程；数据库循环与 SDK WS 线程隔离。"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.bus import leases
from coreman.core.bus.notify import Listener, asyncpg_dsn
from coreman.core.config import get_settings
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot
from coreman.core.db.session import make_session_factory
from coreman.core.logging import configure_logging, get_logger
from coreman.core.platforms.feishu import FeishuClient, FeishuError
from coreman.runtime.gateway_common.inbound import enqueue_inbound
from coreman.runtime.gateway_feishu.channel import DurableChannel
from coreman.runtime.gateway_feishu.inbound import normalize_event
from coreman.runtime.gateway_feishu.transport import FeishuTransport, LeaseLost


@dataclass
class BotInfo:
    id: uuid.UUID
    bot_key: str
    welcome_message: str | None


# 子进程连接池：出站那一路一条会话，入站 ack 那一路一条会话。常驻的应用独占锁连接走单独的
# NullPool 引擎、不占这两个名额，出站锁也由它持有，出站就不必再占一条池连接跨越整轮。
# 这条连接是 AUTOCOMMIT 并使用会话级咨询锁：它若停在一个不结束的事务里，迁移中的在线建索引
# （CREATE INDEX CONCURRENTLY）和 VACUUM 都会一直等它，滚动升级就卡在迁移上。
POOL_SIZE, MAX_OVERFLOW = 1, 1
# 没有本 bot 的通知时多久兜底跑一轮：卡片 9 分钟转静态、失败退避到点都靠它。
IDLE_POLL_SECONDS = 3.0
ERROR_BACKOFF_SECONDS = 5.0
WAKE_CHANNELS = ("stream_updated", "outbox_added")


async def wait_for_work(
    listener: Listener, bot_id: uuid.UUID, stop: asyncio.Event, poll_seconds: float
) -> None:
    """睡到本 bot 有流 / 出站通知、收到停止信号，或兜底轮询到点为止。

    所有飞书子进程都听同样的通道：别的 bot 的通知不算数，继续睡满剩下的时间。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + poll_seconds
    target = str(bot_id)
    while not stop.is_set():
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        waiters = [asyncio.create_task(listener.wait(ch, remaining)) for ch in WAKE_CHANNELS]
        stopper = asyncio.create_task(stop.wait())
        done, pending = await asyncio.wait([*waiters, stopper], return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        payloads = [p for task in waiters if task in done for p in task.result()]
        if any(p.get("bot_id") == target for p in payloads):
            return


async def _sleep_unless_stopped(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def run_child(bot_id: uuid.UUID, instance_id: str, generation: int, parent_pid: int) -> None:
    cfg = get_settings()
    configure_logging(service="gateway-feishu-child", instance=instance_id, level=cfg.log_level)
    log = get_logger(__name__).bind(bot_id=str(bot_id))
    engine = create_async_engine(
        cfg.database_url,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_pre_ping=True,
        hide_parameters=True,
    )
    guard_engine = create_async_engine(
        cfg.database_url,
        poolclass=NullPool,
        hide_parameters=True,
        isolation_level="AUTOCOMMIT",
    )
    factory = make_session_factory(engine)
    listener: Listener | None = None
    app_guard: AsyncConnection | None = None
    client: FeishuClient | None = None
    channel: DurableChannel | None = None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        async with factory() as session:
            bot = await session.get(Bot, bot_id)
            if bot is None or not bot.enabled or bot.platform != "feishu":
                return
            credentials = decrypt_json(
                Cipher(cfg.master_key_bytes), bot.credentials_enc, CREDENTIALS_AAD
            )
            initial_credentials = bot.credentials_enc
            info = BotInfo(bot.id, bot.bot_key, bot.welcome_message)
        app_id, secret = credentials["app_id"], credentials["app_secret"]
        app_guard = await guard_engine.connect()
        exclusive = await app_guard.scalar(
            text("SELECT pg_try_advisory_lock(hashtextextended(:key,0))"),
            {"key": f"feishu-app-connection:{app_id}"},
        )
        if not exclusive:
            raise RuntimeError("application already connected by another bot")
        client = FeishuClient(app_id, secret)
        transport = FeishuTransport(
            factory,
            client,
            bot_id=bot_id,
            instance_id=instance_id,
            generation=generation,
            guard=app_guard,
        )
        await transport.fence()

        async def accept(raw: dict[str, Any]) -> None:
            await transport.fence()
            assert channel is not None
            identity = channel.bot_identity
            bot_open_id = str(identity.open_id if identity else "")
            raw_message = (raw.get("event") or {}).get("message") or {}
            if raw_message.get("chat_type") == "group" and not bot_open_id:
                raise RuntimeError("bot identity not ready")
            message = normalize_event(
                raw,
                bot_id=bot_id,
                app_id=app_id,
                bot_open_id=bot_open_id,
                gateway_instance=instance_id,
                now=datetime.now(UTC),
            )
            if message is None:
                return
            async with factory() as session:
                await session.execute(text("SET LOCAL statement_timeout = 1500"))
                await enqueue_inbound(session, info, message, lease_generation=generation)
                await session.commit()

        channel = DurableChannel(
            accept=accept,
            app_id=app_id,
            app_secret=secret,
            encrypt_key=credentials.get("encrypt_key", ""),
            verification_token=credentials.get("verification_token", ""),
        )
        await channel.start_background(timeout=30)
        async with factory() as session:
            await leases.set_state(session, bot_id, "subscribed", instance_id=instance_id)
            await session.commit()
        listener = Listener(asyncpg_dsn(cfg.database_url), WAKE_CHANNELS)
        await listener.start()
        while not stop.is_set():
            if os.getppid() != parent_pid:
                break
            # 独占锁连接的探活放在 try 外面：它断了独占就没了，只能退出让 supervisor 重启。
            await app_guard.execute(text("SELECT 1"))
            try:
                async with factory() as session:
                    current = await session.get(Bot, bot_id)
                if current is None or current.credentials_enc != initial_credentials:
                    break
                info.welcome_message = current.welcome_message
                more = await transport.round()  # 租约围栏在这一轮开头查一次
            except LeaseLost:
                raise
            except FeishuError as exc:
                log.warning("feishu_delivery_failed", code=exc.code)
                await _sleep_unless_stopped(stop, ERROR_BACKOFF_SECONDS)
                continue
            except Exception as exc:  # noqa: BLE001 库抖动、SDK 之外的异常：记一笔下一轮再来
                # 只记异常类型：消息里可能带 SQL 参数或平台响应。
                log.error("feishu_child_round_failed", error=type(exc).__name__)
                await _sleep_unless_stopped(stop, ERROR_BACKOFF_SECONDS)
                continue
            if not more:
                await wait_for_work(listener, bot_id, stop, IDLE_POLL_SECONDS)
    except LeaseLost:
        log.info("feishu_child_lease_lost")
    finally:
        if channel:
            try:
                await asyncio.wait_for(channel.disconnect(), timeout=5)
            except (TimeoutError, RuntimeError):
                pass
        if client:
            await client.aclose()
        if listener:
            await listener.stop()
        if app_guard:
            await app_guard.close()
        await engine.dispose()
        await guard_engine.dispose()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot-id", required=True, type=uuid.UUID)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--generation", required=True, type=int)
    parser.add_argument("--parent-pid", required=True, type=int)
    args = parser.parse_args()
    try:
        asyncio.run(run_child(args.bot_id, args.instance_id, args.generation, args.parent_pid))
    except Exception:
        # 子进程默认 traceback 可能携带 SDK 响应；这里只报告失败类型的稳定事件。
        get_logger(__name__).error("feishu_child_failed")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

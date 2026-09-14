"""每应用独立 SDK 进程；数据库循环与 SDK WS 线程隔离。"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.config import get_settings
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot
from coreman.core.db.session import make_session_factory
from coreman.core.logging import configure_logging, get_logger
from coreman.core.platforms.feishu import FeishuClient, FeishuError
from coreman.runtime.bus import leases
from coreman.runtime.gateway_feishu.channel import DurableChannel
from coreman.runtime.gateway_feishu.inbound import normalize_event
from coreman.runtime.gateway_feishu.transport import FeishuTransport, LeaseLost
from coreman.runtime.gateway_wecom.inbound import enqueue_inbound


@dataclass
class BotInfo:
    id: uuid.UUID
    bot_key: str
    welcome_message: str | None


async def run_child(bot_id: uuid.UUID, instance_id: str, generation: int, parent_pid: int) -> None:
    cfg = get_settings()
    configure_logging(service="gateway-feishu-child", instance=instance_id, level=cfg.log_level)
    log = get_logger(__name__).bind(bot_id=str(bot_id))
    engine = create_async_engine(
        cfg.database_url, pool_size=2, max_overflow=3, pool_pre_ping=True, hide_parameters=True
    )
    factory = make_session_factory(engine)
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
        app_guard = await engine.connect()
        exclusive = await app_guard.scalar(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"feishu-app-connection:{app_id}"},
        )
        if not exclusive:
            raise RuntimeError("application already connected by another bot")
        client = FeishuClient(app_id, secret)
        transport = FeishuTransport(
            factory, client, bot_id=bot_id, instance_id=instance_id, generation=generation
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
        while not stop.is_set():
            if os.getppid() != parent_pid:
                break
            await app_guard.execute(text("SELECT 1"))
            await transport.fence()
            async with factory() as session:
                current = await session.get(Bot, bot_id)
                if current is None or current.credentials_enc != initial_credentials:
                    break
                info.welcome_message = current.welcome_message
            try:
                await transport.round()
            except FeishuError as exc:
                log.warning("feishu_delivery_failed", code=exc.code)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=5)
                except TimeoutError:
                    pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.3)
            except TimeoutError:
                pass
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
        if app_guard:
            await app_guard.close()
        await engine.dispose()
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

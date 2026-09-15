"""可信部署容器内的运行时排空工具；只输出状态，不输出配置或任务内容。"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.bus import instances
from coreman.core.config import get_settings
from coreman.core.db.models import Bot, BotLease, ProcessInstance, Task
from coreman.core.db.session import make_engine, make_session_factory

PREFIX = re.compile(r"(?:worker-[ab]|gateway-(?:wecom|feishu)-[ab]):[A-Za-z0-9_.-]+:\Z")


def validate_prefix(prefix: str) -> None:
    if not PREFIX.fullmatch(prefix):
        raise ValueError("invalid managed instance prefix")


async def ready(factory: async_sessionmaker[AsyncSession], prefix: str) -> bool:
    validate_prefix(prefix)
    async with factory() as session:
        rows = list(
            await session.scalars(
                select(ProcessInstance).where(
                    ProcessInstance.id.startswith(prefix, autoescape=True),
                    ProcessInstance.stopped_at.is_(None),
                    ProcessInstance.drain_requested_at.is_(None),
                    ProcessInstance.heartbeat_at > datetime.now(UTC) - timedelta(seconds=30),
                )
            )
        )
        return len(rows) == 1


async def wait_ready(
    factory: async_sessionmaker[AsyncSession], prefix: str, wait_seconds: float
) -> None:
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if await ready(factory, prefix):
            return
        await asyncio.sleep(1)
    raise TimeoutError("replacement instance did not become ready")


async def drain(
    factory: async_sessionmaker[AsyncSession],
    prefix: str,
    wait_seconds: float,
    replacement: str | None = None,
) -> None:
    validate_prefix(prefix)
    if replacement:
        if replacement == prefix or not await ready(factory, replacement):
            raise ValueError("replacement must be a different ready instance")
    async with factory() as session:
        old = list(
            await session.scalars(
                select(ProcessInstance.id).where(
                    ProcessInstance.id.startswith(prefix, autoescape=True),
                    ProcessInstance.stopped_at.is_(None),
                )
            )
        )
        bots = list(
            await session.scalars(select(BotLease.bot_id).where(BotLease.holder_instance.in_(old)))
        )
        for identity in old:
            await instances.request_drain(session, identity)
        await session.commit()
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        async with factory() as session:
            alive = await session.scalar(
                select(ProcessInstance.id)
                .where(ProcessInstance.id.in_(old), ProcessInstance.stopped_at.is_(None))
                .limit(1)
            )
            task = await session.scalar(
                select(Task.id)
                .where(Task.claimed_by.in_(old), Task.status.in_(("claimed", "running")))
                .limit(1)
            )
            held = await session.scalar(
                select(BotLease.bot_id).where(BotLease.holder_instance.in_(old)).limit(1)
            )
            pending = False
            if replacement and bots:
                for holder, state in await session.execute(
                    select(BotLease.holder_instance, BotLease.connection_state)
                    .join(Bot, Bot.id == BotLease.bot_id)
                    .where(BotLease.bot_id.in_(bots), Bot.enabled)
                ):
                    pending |= (
                        not holder or not holder.startswith(replacement) or state != "subscribed"
                    )
            if not alive and not task and not held and not pending:
                if replacement and not await ready(factory, replacement):
                    raise RuntimeError("replacement lost readiness")
                return
        await asyncio.sleep(1)
    raise TimeoutError("drain or lease handoff timed out; old work was not killed")


async def run(args: argparse.Namespace) -> None:
    engine = make_engine(get_settings().database_url)
    factory = make_session_factory(engine)
    try:
        if args.command == "ready":
            await wait_ready(factory, args.prefix, args.timeout)
        else:
            await drain(factory, args.prefix, args.timeout, args.replacement)
        print(json.dumps({"status": "ok", "operation": args.command}))
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["ready", "drain"])
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--replacement")
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    if not 0 < args.timeout <= 7500:
        parser.error("timeout must be in (0, 7500]")
    try:

        async def bounded() -> None:
            async with asyncio.timeout(args.timeout + 5):
                await run(args)

        asyncio.run(bounded())
    except Exception as exc:
        print(
            json.dumps({"status": "failed", "operation": args.command, "error": type(exc).__name__})
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

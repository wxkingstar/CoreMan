"""个人助理应用的 SDK 子进程：一个应用一条长连接，进程内按人分发。

与 `child.run_child` 同一副骨架（数据库循环与 SDK WS 线程隔离、应用独占锁、租约围栏、出站
循环），差别在于它不绑定某一个 bot：入站按发送人找助理记录（`AssistantInbound`），出站
发送该应用下所有助理记录的消息（`AppOutbound`），凭证与存活都看 platform_apps 那一行。
"""

from __future__ import annotations

import asyncio
import multiprocessing
import signal
import uuid

from lark_oapi.ws import client as ws_client
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coreman.core.assistants.apps import app_credentials, connection_fingerprint, is_assistant_app
from coreman.core.bus import app_leases
from coreman.core.bus.notify import Listener, asyncpg_dsn
from coreman.core.config import get_settings
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, PlatformApp
from coreman.core.db.session import make_session_factory
from coreman.core.logging import configure_logging, get_logger
from coreman.core.platforms.feishu import FeishuClient, FeishuError
from coreman.runtime.gateway_feishu.app_outbound import AppOutbound
from coreman.runtime.gateway_feishu.assistant_inbound import AssistantInbound
from coreman.runtime.gateway_feishu.channel import DurableChannel
from coreman.runtime.gateway_feishu.child import (
    ERROR_BACKOFF_SECONDS,
    IDLE_POLL_SECONDS,
    MAX_OVERFLOW,
    POOL_SIZE,
    WAKE_CHANNELS,
    _sleep_unless_stopped,
    wait_for_notice,
)
from coreman.runtime.gateway_feishu.transport import LeaseLost


async def app_state(
    factory: async_sessionmaker[AsyncSession], cipher: Cipher, app_id: uuid.UUID
) -> tuple[str | None, set[str]]:
    """应用当前的连接指纹（已删除或不该再连时为 None）与它名下启用的助理记录 id。

    指纹用来发现密钥变更；id 集合用来过滤唤醒通知（通知的载荷是 bot_id）。
    """
    async with factory() as session:
        app = await session.get(PlatformApp, app_id)
        if app is None or not is_assistant_app(app):
            return None, set()
        owned = await session.scalars(
            select(Bot.id).where(
                Bot.platform_app_id == app_id, Bot.kind == "assistant", Bot.enabled.is_(True)
            )
        )
        return connection_fingerprint(cipher, app), {str(bot_id) for bot_id in owned}


def open_database(
    database_url: str,
) -> tuple[AsyncEngine, AsyncEngine, async_sessionmaker[AsyncSession]]:
    """本进程的两个引擎与会话工厂：有池的业务引擎、独占锁连接用的无池引擎。

    会话工厂走 `make_session_factory`：助理的内容列写库前封存、读出后还原都挂在它建出的
    会话上，这里不能另建一个不带封存的工厂。
    """
    engine = create_async_engine(
        database_url,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_pre_ping=True,
        hide_parameters=True,
    )
    guard_engine = create_async_engine(
        database_url,
        poolclass=NullPool,
        hide_parameters=True,
        isolation_level="AUTOCOMMIT",
    )
    return engine, guard_engine, make_session_factory(engine)


def wire(
    factory: async_sessionmaker[AsyncSession],
    client: FeishuClient,
    *,
    cipher: Cipher,
    app_id: uuid.UUID,
    feishu_app_id: str,
    instance_id: str,
    generation: int,
    guard: AsyncConnection | None,
) -> tuple[AppOutbound, AssistantInbound]:
    """出站与入站两半：共用会话工厂、客户端与应用租约的围栏。"""
    outbound = AppOutbound(
        factory,
        client,
        app=app_id,
        instance_id=instance_id,
        generation=generation,
        guard=guard,
    )
    inbound = AssistantInbound(
        factory=factory,
        client=client,
        app=app_id,
        feishu_app_id=feishu_app_id,
        instance_id=instance_id,
        generation=generation,
        cipher=cipher,
        fence=outbound.fence,
    )
    return outbound, inbound


async def run_app(app_id: uuid.UUID, instance_id: str, generation: int) -> None:
    # 与 run_child 相同：看 multiprocessing 留给子进程的 supervisor 哨兵。
    supervisor = multiprocessing.parent_process()
    assert supervisor is not None  # 只由 supervisor 的 `spawn` 拉起
    cfg = get_settings()
    configure_logging(service="gateway-feishu-app", instance=instance_id, level=cfg.log_level)
    log = get_logger(__name__).bind(app=str(app_id))
    engine, guard_engine, factory = open_database(cfg.database_url)
    listener: Listener | None = None
    app_guard: AsyncConnection | None = None
    client: FeishuClient | None = None
    channel: DurableChannel | None = None
    inbound: AssistantInbound | None = None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        cipher = cfg.build_cipher()
        async with factory() as session:
            app = await session.get(PlatformApp, app_id)
            if app is None or not is_assistant_app(app):
                return
            credentials = app_credentials(cipher, app)
            initial = connection_fingerprint(cipher, app)
        feishu_app_id, secret = credentials["app_id"], credentials["app_secret"]
        app_guard = await guard_engine.connect()
        exclusive = await app_guard.scalar(
            text("SELECT pg_try_advisory_lock(hashtextextended(:key,0))"),
            {"key": f"feishu-app-connection:{feishu_app_id}"},
        )
        if not exclusive:
            raise RuntimeError("application already connected")
        client = FeishuClient(feishu_app_id, secret)
        outbound, inbound = wire(
            factory,
            client,
            cipher=cipher,
            app_id=app_id,
            feishu_app_id=feishu_app_id,
            instance_id=instance_id,
            generation=generation,
            guard=app_guard,
        )
        await outbound.fence()
        channel = DurableChannel(accept=inbound.accept, app_id=feishu_app_id, app_secret=secret)
        await channel.start_background(timeout=30)
        async with factory() as session:
            await app_leases.set_state(session, app_id, "subscribed", instance_id=instance_id)
            await session.commit()
        listener = Listener(asyncpg_dsn(cfg.database_url), WAKE_CHANNELS)
        await listener.start()
        owned: set[str] = set()
        while not stop.is_set():
            if not supervisor.is_alive():
                break
            # 独占锁连接的探活放在 try 外面：它断了独占就没了，只能退出让 supervisor 重启。
            await app_guard.execute(text("SELECT 1"))
            try:
                current, owned = await app_state(factory, cipher, app_id)
                if current != initial:
                    break  # 密钥变了、应用停用或删除：退出，由 supervisor 决定重连还是停止
                more = await outbound.round()  # 应用租约的围栏在这一轮开头查一次
            except LeaseLost:
                raise
            except FeishuError as exc:
                log.warning("feishu_delivery_failed", code=exc.code)
                await _sleep_unless_stopped(stop, ERROR_BACKOFF_SECONDS)
                continue
            except Exception as exc:  # noqa: BLE001 库抖动、SDK 之外的异常：记一笔下一轮再来
                # 只记异常类型：消息里可能带 SQL 参数或平台响应。
                log.error("feishu_app_child_round_failed", error=type(exc).__name__)
                await _sleep_unless_stopped(stop, ERROR_BACKOFF_SECONDS)
                continue
            if not more:
                await wait_for_notice(listener, owned.__contains__, stop, IDLE_POLL_SECONDS)
    except LeaseLost:
        log.info("feishu_app_child_lease_lost")
    finally:
        if channel:
            try:
                await asyncio.wait_for(channel.disconnect(), timeout=5)
            except (TimeoutError, RuntimeError):
                pass
        if inbound:
            await inbound.aclose()
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


def run_process(app_id: uuid.UUID, instance_id: str, generation: int) -> None:
    """forkserver 子进程入口（supervisor 的 `spawn`，`child.kind == "app"`）。"""
    # 预加载时关掉了 SDK 的模块级事件循环（见 preload），本进程的长连接另建一个。
    ws_client.loop = asyncio.new_event_loop()
    try:
        asyncio.run(run_app(app_id, instance_id, generation))
    except Exception:
        # 子进程默认 traceback 可能携带 SDK 响应；这里只报告失败类型的稳定事件。
        get_logger(__name__).error("feishu_app_child_failed")
        raise SystemExit(1) from None

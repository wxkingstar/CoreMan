import asyncio

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.runtime.bus.notify import Listener, asyncpg_dsn, notify


def test_asyncpg_dsn() -> None:
    assert asyncpg_dsn("postgresql+asyncpg://u:p@h:5/db") == "postgresql://u:p@h:5/db"
    assert asyncpg_dsn("postgresql://u:p@h:5/db") == "postgresql://u:p@h:5/db"


async def test_notify_delivered_only_after_commit(
    db_engine: AsyncEngine, db_session: AsyncSession, migrated_database: str
) -> None:
    listener = Listener(asyncpg_dsn(migrated_database), ["tasks_queued", "task_cancel"])
    await listener.start()
    try:
        await notify(db_session, "tasks_queued", {"lane": "normal", "bot_id": "x"})
        assert await listener.wait("tasks_queued", timeout=0.3) == []
        await db_session.commit()
        got = await listener.wait("tasks_queued", timeout=2.0)
        assert got == [{"lane": "normal", "bot_id": "x"}]
        assert await listener.wait("tasks_queued", timeout=0.1) == []
        assert await listener.wait("task_cancel", timeout=0.1) == []
    finally:
        await listener.stop()


async def test_listener_reconnects(db_session: AsyncSession, migrated_database: str) -> None:
    listener = Listener(asyncpg_dsn(migrated_database), ["outbox_added"], reconnect_base=0.05)
    await listener.start()
    try:
        assert listener.connected
        await listener._conn.close()  # 模拟断线
        for _ in range(50):
            if listener.connected and listener._conn is not None and not listener._conn.is_closed():
                break
            await asyncio.sleep(0.05)
        assert listener.connected
        await notify(db_session, "outbox_added", {"bot_id": "b"})
        await db_session.commit()
        assert await listener.wait("outbox_added", timeout=2.0) == [{"bot_id": "b"}]
    finally:
        await listener.stop()


class _SilentlyDeadConn:
    """连接被对端悄悄掐掉的样子：没关闭、不报错，就是再也收不到通知。"""

    def __init__(self) -> None:
        self.probes = 0

    def is_closed(self) -> bool:
        return False

    async def fetchval(self, *_args: object) -> object:
        self.probes += 1
        raise OSError("connection is dead")

    async def close(self, **_kw: object) -> None:  # asyncpg 的签名带 timeout，用例不关心
        return None


async def test_listener_probe_detects_a_silently_dead_connection(
    db_session: AsyncSession, migrated_database: str
) -> None:
    """只看 is_closed() 的话这种连接永远「活着」，所有靠通知唤醒的循环就只剩兜底轮询了。"""
    listener = Listener(
        asyncpg_dsn(migrated_database), ["outbox_added"], reconnect_base=0.05, probe_interval=0.0
    )
    await listener.start()
    real = listener._conn
    try:
        dead = _SilentlyDeadConn()
        listener._conn = dead  # type: ignore[assignment]
        # 真连接换下来就关掉：留着它同样会收到通知，用例就分不清是谁收的了
        assert real is not None
        await real.close()
        for _ in range(100):
            if listener.connected and listener._conn is not dead:
                break
            await asyncio.sleep(0.05)
        assert dead.probes >= 1 and listener.connected and listener._conn is not dead
        # 新连接照常收通知
        await notify(db_session, "outbox_added", {"bot_id": "b"})
        await db_session.commit()
        assert await listener.wait("outbox_added", timeout=2.0) == [{"bot_id": "b"}]
    finally:
        if real is not None and not real.is_closed():
            await real.close()
        await listener.stop()

"""make_engine 的建连 / 单语句超时：挂死的语句要被拦下，调用方可以关掉。"""

from __future__ import annotations

import time
from typing import Any

import pytest
from sqlalchemy import text

from coreman.core.db import session as db_session_module
from coreman.core.db.session import make_engine


def test_defaults_bound_connect_and_statement_time(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_create(url: str, **kwargs: Any) -> object:
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(db_session_module, "create_async_engine", fake_create)
    make_engine("postgresql+asyncpg://u:p@127.0.0.1:1/x")
    assert seen["connect_args"] == {"timeout": 10.0, "command_timeout": 300.0}
    make_engine("postgresql+asyncpg://u:p@127.0.0.1:1/x", command_timeout=None)
    assert seen["connect_args"]["command_timeout"] is None


async def test_statement_over_command_timeout_is_cancelled(database_url: str) -> None:
    engine = make_engine(database_url, command_timeout=0.5)
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT pg_sleep(5)"))
        assert time.monotonic() - started < 4
        # 超时后池子照常可用：被取消的连接不会把后续语句带坏
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT 1"))).scalar() == 1
    finally:
        await engine.dispose()


async def test_command_timeout_can_be_disabled_for_long_statements(database_url: str) -> None:
    engine = make_engine(database_url, command_timeout=None)
    try:
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT pg_sleep(1.2), 1"))).one()[1] == 1
    finally:
        await engine.dispose()


async def test_unreachable_database_fails_within_connect_timeout() -> None:
    # 198.51.100.0/24 是文档保留段（TEST-NET-2），SYN 没人应答，只能靠建连超时收场
    engine = make_engine("postgresql+asyncpg://u:p@198.51.100.1:5432/x", connect_timeout=0.5)
    try:
        started = time.monotonic()
        with pytest.raises((TimeoutError, OSError)):
            async with engine.connect():
                pass
        assert time.monotonic() - started < 5
    finally:
        await engine.dispose()

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from coreman.api.errors import ApiError
from coreman.core.contacts.runner import execute_run, start_run
from coreman.core.contacts.types import Directory, DirectoryDept, DirectoryUser
from coreman.core.crypto import Cipher
from coreman.core.db.models import ContactSyncRun, PlatformApp, User
from coreman.core.db.session import make_session_factory
from coreman.core.platforms.wecom import WeComClient, WeComError

KEY = b"\x07" * 32


async def _app(factory) -> PlatformApp:  # type: ignore[no-untyped-def]
    async with factory() as s:
        app = PlatformApp(
            platform="wecom",
            name="sync",
            capabilities=["contact_sync"],
            corp_id="ww1",
            secret_enc=Cipher(KEY).encrypt("sec", "platform_apps.secret_enc"),
        )
        s.add(app)
        await s.commit()
        await s.refresh(app)
        return app


async def test_run_success_and_conflict(db_engine: AsyncEngine) -> None:
    factory = make_session_factory(db_engine)
    app = await _app(factory)
    run = await start_run(factory, app.id, None)
    with pytest.raises(ApiError) as ei:
        await start_run(factory, app.id, None)
    assert ei.value.status_code == 409

    async def fake_fetch(client: WeComClient) -> Directory:
        assert client._secret == "sec"
        return Directory(
            "wecom",
            [DirectoryDept("1", None, "公司", 1)],
            [DirectoryUser("a", "A", ["1"], "1", None, None, None, None, True, {})],
        )

    done = await execute_run(factory, Cipher(KEY), run.id, fetch=fake_fetch)
    assert done.status == "success" and done.stats["created"] == 1 and done.finished_at is not None
    async with factory() as s:
        assert (await s.execute(select(User).where(User.login_name == "a"))).scalar_one()


async def test_stale_run_marked_failed(db_engine: AsyncEngine) -> None:
    factory = make_session_factory(db_engine)
    app = await _app(factory)
    async with factory() as s:
        s.add(
            ContactSyncRun(
                platform_app_id=app.id,
                status="running",
                started_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        await s.commit()
    run = await start_run(factory, app.id, None)
    async with factory() as s:
        rows = (await s.execute(select(ContactSyncRun).order_by(ContactSyncRun.id))).scalars().all()
    assert [r.status for r in rows] == ["failed", "running"] and rows[1].id == run.id


async def test_wecom_error_48009_message(db_engine: AsyncEngine) -> None:
    factory = make_session_factory(db_engine)
    app = await _app(factory)
    run = await start_run(factory, app.id, None)

    async def boom(client: WeComClient) -> Directory:
        raise WeComError(48009, "api forbidden")

    done = await execute_run(factory, Cipher(KEY), run.id, fetch=boom)
    assert done.status == "failed"
    assert "48009" in (done.error or "") and "可信 IP" in (done.error or "")


async def test_decrypt_failure_finalizes_run(db_engine: AsyncEngine) -> None:
    factory = make_session_factory(db_engine)
    app = await _app(factory)
    run = await start_run(factory, app.id, None)
    done = await execute_run(factory, Cipher(b"\x09" * 32), run.id)  # 错误的密钥 → 解密失败
    assert done.status == "failed"
    assert done.error is not None and done.error.startswith("DecryptError")
    assert "sec" not in done.error and done.finished_at is not None

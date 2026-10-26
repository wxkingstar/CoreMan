"""通讯录定时全量同步（scheduler）与 `coreman contact-sync` 的跨平台选应用。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from coreman import cli
from coreman.core.contacts import runner
from coreman.core.contacts.types import Directory, DirectoryDept, DirectoryUser
from coreman.core.crypto import Cipher
from coreman.core.db.models import ContactSyncRun, PlatformApp, User
from coreman.core.db.session import make_session_factory
from coreman.core.platforms.feishu import FeishuClient
from coreman.core.platforms.wecom import WeComClient
from coreman.runtime.scheduler.contacts import run_due
from tests.integration.worker_helpers import MASTER

DAY = timedelta(hours=24)
Factory = async_sessionmaker[AsyncSession]


def _directory(platform: str, uid: str) -> Directory:
    return Directory(
        platform,
        [DirectoryDept("1", None, "公司", 1)],
        [DirectoryUser(uid, uid.upper(), ["1"], "1", None, None, None, None, True, {})],
    )


async def wecom_fetch(client: WeComClient) -> Directory:
    return _directory("wecom", "wecom-user")


@pytest.fixture(autouse=True)
def fake_feishu(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fetch(client: FeishuClient) -> Directory:
        return _directory("feishu", "feishu-user")

    monkeypatch.setattr(runner, "fetch_feishu_directory", fetch)


async def _app(
    factory: Factory,
    name: str,
    platform: str = "wecom",
    *,
    enabled: bool = True,
    capabilities: list[str] | None = None,
) -> PlatformApp:
    async with factory() as s:
        app = PlatformApp(
            platform=platform,
            name=name,
            enabled=enabled,
            capabilities=["contact_sync"] if capabilities is None else capabilities,
            corp_id="ww-demo" if platform == "wecom" else None,
            app_id="cli_demo" if platform == "feishu" else None,
            secret_enc=Cipher(MASTER).encrypt("sec", "platform_apps.secret_enc"),
        )
        s.add(app)
        await s.commit()
        await s.refresh(app)
        return app


async def _runs(factory: Factory, app: PlatformApp) -> list[ContactSyncRun]:
    async with factory() as s:
        stmt = select(ContactSyncRun).where(ContactSyncRun.platform_app_id == app.id)
        return list((await s.scalars(stmt.order_by(ContactSyncRun.id))).all())


async def _past_run(factory: Factory, app: PlatformApp, ago: timedelta, status: str) -> None:
    async with factory() as s:
        s.add(
            ContactSyncRun(
                platform_app_id=app.id, status=status, started_at=datetime.now(UTC) - ago
            )
        )
        await s.commit()


async def test_syncs_due_apps_on_both_platforms(db_engine: AsyncEngine) -> None:
    factory = make_session_factory(db_engine)
    wecom = await _app(factory, "wecom-sync")
    feishu = await _app(factory, "feishu-sync", "feishu")
    recent = await _app(factory, "recent")
    await _past_run(factory, recent, timedelta(hours=1), "success")
    stale = await _app(factory, "stale")
    await _past_run(factory, stale, timedelta(hours=25), "failed")
    off = await _app(factory, "off", enabled=False)
    login_only = await _app(factory, "login-only", capabilities=["login"])

    counts = await run_due(factory, Cipher(MASTER), datetime.now(UTC), DAY, wecom_fetch)

    assert counts["contact_sync_success"] == 3 and counts["contact_sync_skipped"] == 0
    assert [r.status for r in await _runs(factory, wecom)] == ["success"]
    assert [r.status for r in await _runs(factory, feishu)] == ["success"]
    assert len(await _runs(factory, recent)) == 1
    assert [r.status for r in await _runs(factory, stale)] == ["failed", "success"]
    assert await _runs(factory, off) == [] and await _runs(factory, login_only) == []
    async with factory() as s:
        names = set((await s.scalars(select(User.login_name))).all())
    assert {"wecom-user", "feishu-user"} <= names
    # 刚跑完，同一时刻再看一遍不会重跑
    again = await run_due(factory, Cipher(MASTER), datetime.now(UTC), DAY, wecom_fetch)
    assert not any(again.values())


async def test_skips_app_with_sync_in_progress(db_engine: AsyncEngine) -> None:
    """手动同步正在跑（running 行未僵死）时 start_run 返回 409，本轮跳过、不新建记录。"""
    factory = make_session_factory(db_engine)
    app = await _app(factory, "busy")
    await _past_run(factory, app, timedelta(hours=1), "success")
    await _past_run(factory, app, timedelta(minutes=1), "running")

    counts = await run_due(factory, Cipher(MASTER), datetime.now(UTC) + DAY, DAY, wecom_fetch)

    assert counts["contact_sync_skipped"] == 1
    assert [r.status for r in await _runs(factory, app)] == ["success", "running"]


async def test_abort_protection_kept(db_engine: AsyncEngine) -> None:
    """抓到空目录时 ContactSyncService 整体中止，定时同步原样记为 aborted、不停用任何人。"""
    factory = make_session_factory(db_engine)
    app = await _app(factory, "empty")

    async def empty(client: WeComClient) -> Directory:
        return Directory("wecom", [], [])

    counts = await run_due(factory, Cipher(MASTER), datetime.now(UTC), DAY, empty)

    assert counts["contact_sync_aborted"] == 1
    assert [r.status for r in await _runs(factory, app)] == ["aborted"]


@pytest.mark.usefixtures("runtime_settings")
async def test_cli_picks_feishu_app(
    db_engine: AsyncEngine, capsys: pytest.CaptureFixture[str]
) -> None:
    factory = make_session_factory(db_engine)
    feishu = await _app(factory, "feishu-sync", "feishu")
    assert await cli._contact_sync(None) == 0
    assert '"status": "success"' in capsys.readouterr().out
    assert [r.status for r in await _runs(factory, feishu)] == ["success"]

    await _app(factory, "wecom-sync")
    assert await cli._contact_sync(None) == 1
    err = capsys.readouterr().err
    assert "匹配到 2 个" in err and "feishu-sync（feishu）" in err and "wecom-sync（wecom）" in err
    assert await cli._contact_sync("feishu-sync") == 0
    assert len(await _runs(factory, feishu)) == 2

"""对账：翻 Tower 状态接口补上丢失回调的决定，规则与回调一致，游标只在一页落地后推进。"""

from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.cloud_proxy import reconcile, resume
from coreman.core.config import Settings
from coreman.core.db.models import Setting
from coreman.core.db.models.cloud_proxy import CloudPendingApproval
from coreman.core.db.session import make_session_factory
from coreman.core.settings_store import SettingsStore
from tests.api.test_tower_callback import pending_chat, resumes

URL = "https://tower.test"
KEY = "sender-key-for-tests"


def item(external_id: str, status: str = "approved", **kw: object) -> dict[str, object]:
    return {
        "approval_id": kw.pop("approval_id", "apv-1"),
        "external_id": external_id,
        "status": status,
        "reason": kw.pop("reason", None),
        "decided_at": "2026-10-16T10:00:00.123456789Z",
        **kw,
    }


@pytest.fixture
def factory(db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(db_session.bind)  # type: ignore[arg-type]


async def reconcile_once(
    factory: async_sessionmaker[AsyncSession], store: SettingsStore
) -> dict[str, int]:
    async with reconcile.client() as http:
        return await reconcile.run(factory, store, tower_url=URL, sender_key=KEY, http=http)


async def cursor_of(db_session: AsyncSession) -> object:
    db_session.expire_all()
    row = await db_session.get(Setting, reconcile.CURSOR_KEY)
    return row.value if row else None


@respx.mock
async def test_missed_decision_is_applied_once_and_cursor_advances(
    db_session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    await pending_chat(db_session)
    route = respx.get(f"{URL}/v1/approvals").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    item("cm-1-aa", reason="好"),
                    item("other-flow", approval_id="apv-2"),
                    item("cm-unknown", approval_id="apv-3"),
                    item("cm-pending", "pending", approval_id="apv-4"),
                    "garbage",
                ],
                "next_cursor": "c1",
            },
        )
    )
    store = SettingsStore(factory)
    stats = await reconcile_once(factory, store)
    assert stats["matched"] == 1
    req = route.calls[0].request
    assert req.headers["X-API-Key"] == KEY
    assert req.url.params["updated_since"] == ""
    row = await db_session.get(CloudPendingApproval, "cm-1-aa", populate_existing=True)
    assert row.status == "approved" and row.approval_id == "apv-1" and row.reason == "好"
    assert row.resume_task_id is not None
    assert len(await resumes(db_session)) == 1
    assert await cursor_of(db_session) == "c1"
    # 再对一次（同一页重来）：不回退、不重复续接
    await reconcile_once(factory, SettingsStore(factory))
    assert len(await resumes(db_session)) == 1
    assert route.calls[-1].request.url.params["updated_since"] == "c1"


@respx.mock
async def test_terminal_rows_never_regress_and_reason_is_neutralised(
    db_session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    bot, _, _, executed = await pending_chat(db_session, "cm-1-ex")
    executed.status = "executed"
    executed.approval_id = "apv-1"
    # 没有续接上下文的挂起单：只验状态迁移与理由处理
    db_session.add(
        CloudPendingApproval(
            external_id="cm-2-rj",
            bot_id=bot.id,
            session_key="s",
            product="slb",
            action="a",
            request_hash="h",
        )
    )
    await db_session.commit()
    respx.get(f"{URL}/v1/approvals").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    item("cm-1-ex", "rejected", approval_id="apv-1"),
                    item("cm-2-rj", "rejected", approval_id="apv-2", reason="<x>" + "a" * 900),
                    item("cm-2-rj", "mystery-status", approval_id="apv-2"),
                ],
                "next_cursor": "c",
            },
        )
    )
    await reconcile_once(factory, SettingsStore(factory))
    db_session.expire_all()
    assert (await db_session.get(CloudPendingApproval, "cm-1-ex")).status == "executed"
    got = await db_session.get(CloudPendingApproval, "cm-2-rj")
    assert got.status == "rejected"
    assert got.reason is not None and len(got.reason) == resume_limit() and "<" not in got.reason


def resume_limit() -> int:
    from coreman.core.cloud_proxy.resume import REASON_LIMIT

    return REASON_LIMIT


@respx.mock
async def test_paging_stops_on_short_page_and_cursor_survives_failure(
    db_session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    full = [item(f"cm-x{i}", "pending", approval_id=f"a{i}") for i in range(reconcile.PAGE_LIMIT)]
    route = respx.get(f"{URL}/v1/approvals").mock(
        side_effect=[
            httpx.Response(200, json={"items": full, "next_cursor": "p1"}),
            httpx.Response(500),
        ]
    )
    store = SettingsStore(factory)
    with pytest.raises(reconcile.ReconcileError):
        await reconcile_once(factory, store)
    # 第一页已处理：游标停在它后面，失败页不推进
    assert await cursor_of(db_session) == "p1"
    assert route.calls[1].request.url.params["updated_since"] == "p1"

    route.mock(side_effect=[httpx.Response(200, json={"items": [], "next_cursor": "p1"})])
    stats = await reconcile_once(factory, SettingsStore(factory))
    assert stats["pages"] == 1


@respx.mock
async def test_does_not_follow_redirects_or_leak_key(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    respx.get(f"{URL}/v1/approvals").mock(
        return_value=httpx.Response(302, headers={"Location": "https://evil.test/"})
    )
    evil = respx.get("https://evil.test/").mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(reconcile.ReconcileError) as exc:
        await reconcile_once(factory, SettingsStore(factory))
    assert not evil.called and KEY not in str(exc.value)


async def test_cli_requires_tower_settings(
    api_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from coreman import cli

    bare = api_settings.model_copy(update={"tower_url": None, "tower_sender_key": None})
    monkeypatch.setattr(cli, "get_settings", lambda: bare)
    assert await cli._tower_reconcile() == 1
    assert "TOWER_SENDER_KEY" in capsys.readouterr().err


@respx.mock
async def test_item_that_fails_to_apply_does_not_stall_cursor(
    db_session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, _, _, _ = await pending_chat(db_session, "cm-1-ok")
    db_session.add(
        CloudPendingApproval(
            external_id="cm-2-bad",
            bot_id=bot.id,
            session_key="s",
            product="slb",
            action="a",
            request_hash="h",
        )
    )
    await db_session.commit()
    real = resume.start

    async def flaky(session: AsyncSession, row: CloudPendingApproval) -> object:
        if row.external_id == "cm-2-bad":
            raise RuntimeError("boom")
        return await real(session, row)

    monkeypatch.setattr(resume, "start", flaky)
    respx.get(f"{URL}/v1/approvals").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [item("cm-2-bad", approval_id="apv-2"), item("cm-1-ok")],
                "next_cursor": "c9",
            },
        )
    )
    stats = await reconcile_once(factory, SettingsStore(factory))
    assert stats["failed"] == 1 and stats["matched"] == 1
    assert await cursor_of(db_session) == "c9"
    db_session.expire_all()
    assert (await db_session.get(CloudPendingApproval, "cm-1-ok")).status == "approved"
    bad = await db_session.get(CloudPendingApproval, "cm-2-bad")
    assert bad.status == "pending" and bad.approval_id is None

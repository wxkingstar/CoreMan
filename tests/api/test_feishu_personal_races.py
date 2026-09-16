"""Real PostgreSQL transactions prove revocation waits for in-flight grants."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from coreman.core.db.models import FeishuPersonalGrant
from coreman.core.db.session import make_session_factory
from coreman.core.feishu_personal import policy, service
from tests.api.test_feishu_personal import grant, setup


@pytest.mark.parametrize("existing", [False, True], ids=["first-authorization", "token-refresh"])
async def test_revoke_serializes_with_authorize_or_refresh(
    app, db_session, db_engine, monkeypatch, existing
):
    bot, owner, task = await setup(db_session, app)
    if existing:
        await grant(
            db_session, app, bot, owner, expires_at=datetime.now(UTC) - timedelta(seconds=1)
        )
    factory = make_session_factory(db_engine)
    at_http, release_http, revoke_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    revoke_pid = None

    async def http(method, url, **kwargs):
        if method == "POST":
            at_http.set()
            await release_http.wait()
            if existing:
                return {
                    "access_token": "new-access-secret",
                    "refresh_token": "new-refresh-secret",
                    "expires_in": 7200,
                }
            return {
                "device_code": "new-device-secret",
                "expires_in": 600,
                "verification_uri_complete": "https://accounts.feishu.cn/verify?user_code=TEST",
            }
        return {"code": 0, "data": {"minute": {"title": "private content"}}}

    monkeypatch.setattr(service, "_http", http)

    async def authorize_or_refresh():
        async with factory() as session:
            scope = await policy.task_scope(session, task.id, str(owner.id))
            if existing:
                result = await service.api_request(
                    session, app.state.cipher, scope, "GET", "/minutes/v1/minutes/minute-one"
                )
            else:
                result = await service.authorize(session, app.state.cipher, scope)
            await session.commit()
            return result

    async def revoke():
        nonlocal revoke_pid
        async with factory() as session:
            revoke_pid = await session.scalar(text("SELECT pg_backend_pid()"))
            revoke_started.set()
            result = await service.revoke_grant(session, bot.id, owner.id)
            await session.commit()
            return result

    writer = asyncio.create_task(authorize_or_refresh())
    revoker = None
    try:
        async with asyncio.timeout(10):
            await at_http.wait()
            async with factory() as observer:
                visible = await observer.scalar(
                    select(FeishuPersonalGrant).where(
                        FeishuPersonalGrant.bot_id == bot.id,
                        FeishuPersonalGrant.user_id == owner.id,
                    )
                )
                # The first grant has already been inserted by the writer, but is
                # uncommitted and therefore invisible to the revocation transaction.
                assert (visible is not None) == existing
                revoker = asyncio.create_task(revoke())
                await revoke_started.wait()
                while True:
                    blockers = await observer.scalar(
                        text("SELECT pg_blocking_pids(:pid)"), {"pid": revoke_pid}
                    )
                    if blockers:
                        break
                    # Every round trip yields to both real concurrent transactions;
                    # no sleeps or guessed timing decide whether the lock worked.
                    assert not revoker.done(), "revocation escaped the in-flight grant lock"
                assert not revoker.done()
                release_http.set()
                written, revoked = await asyncio.gather(writer, revoker)
                assert revoked == {"status": "revoked"}
                assert (
                    written["minute"]["title"] == "private content"
                    if existing
                    else written["status"] == "pending"
                )
            async with factory() as verify:
                row = await verify.get(FeishuPersonalGrant, (bot.id, owner.id))
                assert row is not None and row.status == "revoked"
                assert row.token_enc is None and row.pending_enc is None
                assert row.expires_at is None and row.pending_expires_at is None
                assert row.next_poll_at is None and row.scopes == []
    finally:
        release_http.set()
        for pending in (writer, revoker):
            if pending is not None and not pending.done():
                pending.cancel()
        await asyncio.gather(
            *(t for t in (writer, revoker) if t is not None), return_exceptions=True
        )

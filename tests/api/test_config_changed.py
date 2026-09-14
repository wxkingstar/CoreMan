import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.chat import sessions
from coreman.core.db.models import Bot, ChatSession
from coreman.runtime.bus.notify import Listener, asyncpg_dsn
from tests.api.conftest import login_as
from tests.api.test_bots import _bot_body, _team


async def test_patch_model_clears_sessions_and_notifies(
    client: httpx.AsyncClient, db_session: AsyncSession, migrated_database: str
) -> None:
    team = await _team(db_session)
    await login_as(client, db_session, role="member", team_id=team.id)
    created = (await client.post("/api/admin/bots", json=_bot_body())).json()["data"]
    bot = (await db_session.execute(select(Bot))).scalar_one()
    await sessions.get_or_create(
        db_session,
        bot_id=bot.id,
        session_key="u1",
        backend="claude",
        ttl_hours=72,
        speaker_user_id=None,
    )
    await db_session.commit()
    listener = Listener(asyncpg_dsn(migrated_database), ["config_changed"])
    await listener.start()
    try:
        r = await client.patch(
            f"/api/admin/bots/{created['id']}",
            json={"name": "改名"},
            headers={"If-Match": f'"{created["version"]}"'},
        )
        assert r.status_code == 200
        assert (
            len((await db_session.execute(select(ChatSession))).scalars().all()) == 1
        )  # 改名不清会话
        got = await listener.wait("config_changed", timeout=2.0)
        assert {"table": "bots", "id": created["id"]} in got
        r = await client.patch(
            f"/api/admin/bots/{created['id']}",
            json={"model": "vllm/claude-opus-4-6"},
            headers={"If-Match": f'"{created["version"] + 1}"'},
        )
        assert r.status_code == 200, r.text
        assert (await db_session.execute(select(ChatSession))).scalars().all() == []
        r = await client.post(f"/api/admin/bots/{created['id']}/toggle")
        assert r.status_code == 200
        got = await listener.wait("config_changed", timeout=2.0)
        assert any(g.get("table") == "bots" for g in got)
    finally:
        await listener.stop()

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import AuditLog, User
from tests.api.conftest import login_as

TEAM = {
    "slug": "backend",
    "name_zh": "服务端",
    "name_ja": "サーバー",
    "name_en": None,
    "sort_order": 1,
    "enabled": True,
}


async def test_team_crud_and_rules(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    await login_as(client, db_session, role="member")
    assert (await client.post("/api/admin/teams", json=TEAM)).status_code == 403
    await login_as(client, db_session, role="ai_committee")
    created = await client.post("/api/admin/teams", json=TEAM)
    assert created.status_code == 201, created.text
    tid = created.json()["data"]["id"]
    assert (
        await client.post("/api/admin/teams", json={**TEAM, "slug": "Bad Slug"})
    ).status_code == 422
    rules = await client.put(
        f"/api/admin/teams/{tid}/rules",
        json=[
            {"platform": None, "dept_path_contains": "服务端", "sort_order": 2},
            {"platform": "wecom", "dept_path_contains": "后端", "sort_order": 1},
        ],
    )
    assert rules.status_code == 200
    assert [r["dept_path_contains"] for r in rules.json()["data"]["rules"]] == ["后端", "服务端"]
    lst = (await client.get("/api/admin/teams")).json()["data"]
    assert lst[0]["slug"] == "backend" and lst[0]["member_count"] == 0 and len(lst[0]["rules"]) == 2
    upd = await client.put(f"/api/admin/teams/{tid}", json={**TEAM, "name_zh": "服务端组"})
    assert upd.status_code == 200 and upd.json()["data"]["name_zh"] == "服务端组"
    db_session.add(User(display_name="成员", team_id=tid))
    await db_session.commit()
    assert (await client.delete(f"/api/admin/teams/{tid}")).status_code == 409
    assert (await client.get("/api/admin/teams")).json()["data"][0]["member_count"] == 1


async def test_duplicate_slug_409_and_delete_audit_diff(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await login_as(client, db_session, role="platform_admin")
    created = await client.post("/api/admin/teams", json=TEAM)
    assert created.status_code == 201
    assert (await client.post("/api/admin/teams", json=TEAM)).status_code == 409
    tid = created.json()["data"]["id"]
    assert (await client.delete(f"/api/admin/teams/{tid}")).status_code == 200
    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "team.delete"))
    ).scalar_one()
    assert audit.diff is not None and audit.diff["slug"] == ["backend", None]


async def test_delete_team_blocked_by_bots(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    from coreman.core.db.models import Bot, Team

    admin = await login_as(client, db_session, role="platform_admin")
    team = Team(slug="t-bots", name_zh="有机器人")
    db_session.add(team)
    await db_session.flush()
    db_session.add(
        Bot(
            bot_key="b1",
            platform="wecom",
            name="b",
            created_by=admin.id,
            team_id=team.id,
            model="m",
            working_dir="/d",
            credentials_enc="enc",
        )
    )
    await db_session.commit()
    r = await client.delete(f"/api/admin/teams/{team.id}")
    assert r.status_code == 409 and "机器人" in r.json()["message"]


async def test_delete_team_blocked_by_relay(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    from coreman.core.db.models import RelayServer, Team

    await login_as(client, db_session, role="platform_admin")
    team = Team(slug="t-relay", name_zh="有实例")
    db_session.add(team)
    await db_session.flush()
    db_session.add(
        RelayServer(
            name="claude02",
            host="10.0.0.2",
            clawrelay_port=50009,
            model_provider="claude",
            team_id=team.id,
        )
    )
    await db_session.commit()
    r = await client.delete(f"/api/admin/teams/{team.id}")
    assert r.status_code == 409 and "运行时" in r.json()["message"]

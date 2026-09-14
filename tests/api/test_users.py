import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import AuditLog, Team, User, UserIdentity
from tests.api.conftest import login_as


async def _teams(db_session: AsyncSession) -> tuple[Team, Team]:
    a, b = Team(slug="a", name_zh="甲"), Team(slug="b", name_zh="乙")
    db_session.add_all([a, b])
    await db_session.commit()
    return a, b


async def test_list_filters(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    a, _ = await _teams(db_session)
    me = await login_as(client, db_session, role="member")
    zs = User(login_name="zhangsan", display_name="张三", email="zs@example.com", team_id=a.id)
    db_session.add_all([zs, User(login_name="lisi", display_name="李四", status="disabled")])
    await db_session.flush()
    db_session.add(UserIdentity(user_id=zs.id, platform="wecom", platform_user_id="zhangsan"))
    await db_session.commit()
    r = await client.get("/api/admin/users", params={"keyword": "zs@"})
    assert r.status_code == 200 and [u["login_name"] for u in r.json()["data"]["items"]] == [
        "zhangsan"
    ]
    item = r.json()["data"]["items"][0]
    assert item["team_name"] == "甲" and item["identities"] == [
        {"platform": "wecom", "platform_user_id": "zhangsan"}
    ]
    assert {
        u["login_name"]
        for u in (await client.get("/api/admin/users", params={"unassigned": "true"})).json()[
            "data"
        ]["items"]
    } == {me.login_name, "lisi"}
    assert (await client.get("/api/admin/users", params={"status": "disabled"})).json()["data"][
        "total"
    ] == 1


async def test_patch_permissions(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    a, b = await _teams(db_session)
    target = User(login_name="t", display_name="目标", team_id=a.id)
    db_session.add(target)
    await db_session.commit()
    # member 403
    await login_as(client, db_session, role="member")
    assert (
        await client.patch(f"/api/admin/users/{target.id}", json={"position": "x"})
    ).status_code == 403
    # team_lead 本团队可改，不能提为 ai_committee，不能改别的团队
    lead = await login_as(client, db_session, role="team_lead", team_id=a.id)
    ok = await client.patch(
        f"/api/admin/users/{target.id}", json={"role": "team_lead", "position": "研发"}
    )
    assert (
        ok.status_code == 200
        and ok.json()["data"]["role"] == "team_lead"
        and ok.json()["data"]["manual_fields"] == ["position"]
    )
    assert (
        await client.patch(f"/api/admin/users/{target.id}", json={"role": "ai_committee"})
    ).status_code == 403
    await db_session.refresh(target)
    target.team_id = b.id
    await db_session.commit()
    assert (
        await client.patch(f"/api/admin/users/{target.id}", json={"position": "y"})
    ).status_code == 403
    assert (
        await client.patch(f"/api/admin/users/{lead.id}", json={"role": "member"})
    ).status_code == 403  # 不能改自己的角色
    # ai_committee 可改任意团队，不能设 platform_admin
    await login_as(client, db_session, role="ai_committee")
    assert (
        await client.patch(
            f"/api/admin/users/{target.id}", json={"team_id": str(a.id), "bot_accessible": False}
        )
    ).status_code == 200
    assert (
        await client.patch(f"/api/admin/users/{target.id}", json={"role": "platform_admin"})
    ).status_code == 403
    # platform_admin 全量；空 body 422；引导用户不可改
    await login_as(client, db_session, role="platform_admin")
    assert (
        await client.patch(
            f"/api/admin/users/{target.id}", json={"role": "platform_admin", "status": "disabled"}
        )
    ).status_code == 200
    assert (await client.patch(f"/api/admin/users/{target.id}", json={})).status_code == 422
    boot = User(display_name="引导", role="platform_admin", source="bootstrap")
    db_session.add(boot)
    await db_session.commit()
    assert (
        await client.patch(f"/api/admin/users/{boot.id}", json={"locale": "ja"})
    ).status_code == 403
    audits = (
        (await db_session.execute(select(AuditLog).where(AuditLog.action == "user.update")))
        .scalars()
        .all()
    )
    assert len(audits) == 3 and audits[0].diff == {
        "position": [None, "研发"],
        "role": ["member", "team_lead"],
    }


async def test_team_lead_cannot_move_member_to_other_team(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    a, b = await _teams(db_session)
    target = User(login_name="t2", display_name="目标2", team_id=a.id)
    db_session.add(target)
    await db_session.commit()
    await login_as(client, db_session, role="team_lead", team_id=a.id)
    assert (
        await client.patch(f"/api/admin/users/{target.id}", json={"team_id": str(b.id)})
    ).status_code == 403
    assert (
        await client.patch(
            f"/api/admin/users/{target.id}", json={"team_id": str(b.id), "role": "team_lead"}
        )
    ).status_code == 403
    assert (
        await client.patch(f"/api/admin/users/{target.id}", json={"team_id": None})
    ).status_code == 200
    await db_session.refresh(target)
    assert target.team_id is None


async def test_patch_only_tracks_changed_manual_fields(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """没有变化的字段不能进 manual_fields，否则通讯录同步会永远跳过它。"""
    target = User(login_name="t4", display_name="目标4", locale="zh")
    db_session.add(target)
    await db_session.commit()
    await login_as(client, db_session, role="platform_admin")
    r = await client.patch(
        f"/api/admin/users/{target.id}", json={"position": "研发", "locale": "zh"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["manual_fields"] == ["position"]


async def test_only_admins_can_change_status(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """status 只有 ai_committee/platform_admin 能改；改过之后计入 manual_fields。"""
    a, _ = await _teams(db_session)
    target = User(login_name="t5", display_name="目标5", team_id=a.id)
    db_session.add(target)
    await db_session.commit()
    await login_as(client, db_session, role="team_lead", team_id=a.id)
    assert (
        await client.patch(f"/api/admin/users/{target.id}", json={"status": "disabled"})
    ).status_code == 403
    assert (
        await client.patch(f"/api/admin/users/{target.id}", json={"position": "研发"})
    ).status_code == 200
    await login_as(client, db_session, role="ai_committee")
    r = await client.patch(f"/api/admin/users/{target.id}", json={"status": "disabled"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["manual_fields"] == ["position", "status"]


async def test_patch_unknown_team_is_422(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    target = User(login_name="t3", display_name="目标3")
    db_session.add(target)
    await db_session.commit()
    await login_as(client, db_session, role="platform_admin")
    r = await client.patch(f"/api/admin/users/{target.id}", json={"team_id": str(uuid.uuid4())})
    assert r.status_code == 422

import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.routers.users import PUBLIC_USER_FIELDS
from coreman.core.db.models import AuditLog, Department, Team, User, UserDepartment, UserIdentity
from tests.api.conftest import login_as

# 管理角色额外可见的通讯录明细（前端按这份契约隐藏列）。
DETAIL_FIELDS = {
    "email",
    "mobile",
    "identities",
    "departments",
    "manual_fields",
    "last_login_at",
    "position",
    "skills",
    "bot_accessible",
}


async def _teams(db_session: AsyncSession) -> tuple[Team, Team]:
    a, b = Team(slug="a", name_zh="甲"), Team(slug="b", name_zh="乙")
    db_session.add_all([a, b])
    await db_session.commit()
    return a, b


async def _directory(db_session: AsyncSession, team: Team) -> User:
    """一个带手机号、邮箱、平台身份与部门的同步用户，外加一个停用用户。"""
    zs = User(
        login_name="zhangsan",
        display_name="张三",
        email="zs@example.com",
        mobile="13800000000",
        position="研发",
        team_id=team.id,
    )
    dept = Department(platform="wecom", platform_dept_id="1", name="研发部", path="总部/研发部")
    db_session.add_all([zs, dept, User(login_name="lisi", display_name="李四", status="disabled")])
    await db_session.flush()
    db_session.add_all(
        [
            UserIdentity(user_id=zs.id, platform="wecom", platform_user_id="zhangsan"),
            UserDepartment(user_id=zs.id, department_id=dept.id, is_primary=True),
        ]
    )
    await db_session.commit()
    return zs


async def test_list_filters(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    a, _ = await _teams(db_session)
    me = await login_as(client, db_session, role="ai_committee")
    await _directory(db_session, a)
    r = await client.get("/api/admin/users", params={"keyword": "zs@"})
    assert r.status_code == 200 and [u["login_name"] for u in r.json()["data"]["items"]] == [
        "zhangsan"
    ]
    item = r.json()["data"]["items"][0]
    assert set(item) == set(PUBLIC_USER_FIELDS) | DETAIL_FIELDS
    assert item["team_name"] == "甲" and item["identities"] == [
        {"platform": "wecom", "platform_user_id": "zhangsan"}
    ]
    assert item["mobile"] == "13800000000" and item["departments"] == ["总部/研发部"]
    assert {
        u["login_name"]
        for u in (await client.get("/api/admin/users", params={"unassigned": "true"})).json()[
            "data"
        ]["items"]
    } == {me.login_name, "lisi"}
    assert (await client.get("/api/admin/users", params={"status": "disabled"})).json()["data"][
        "total"
    ] == 1
    assert (await client.get("/api/admin/users", params={"role": "ai_committee"})).json()["data"][
        "total"
    ] == 1
    detail = await client.get(f"/api/admin/users/{item['id']}")
    assert detail.status_code == 200 and detail.json()["data"]["email"] == "zs@example.com"


async def test_member_and_team_lead_get_public_fields_only(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """非管理角色只拿选择器需要的字段：手机号、邮箱、平台身份、部门等一律不回。"""
    a, _ = await _teams(db_session)
    zs = await _directory(db_session, a)
    for role in ("member", "team_lead"):
        await login_as(client, db_session, role=role, team_id=a.id)
        r = await client.get("/api/admin/users", params={"keyword": "张", "per_page": 200})
        assert r.status_code == 200, r.text
        items = r.json()["data"]["items"]
        assert [u["login_name"] for u in items] == ["zhangsan"]
        assert set(items[0]) == set(PUBLIC_USER_FIELDS), role
        assert items[0]["team_name"] == "甲" and items[0]["status"] == "active"
        body = r.text
        assert "13800000000" not in body and "zs@example.com" not in body
        assert "总部/研发部" not in body and "platform_user_id" not in body
        detail = await client.get(f"/api/admin/users/{zs.id}")
        assert detail.status_code == 200 and set(detail.json()["data"]) == set(PUBLIC_USER_FIELDS)
        # 选择器仍可按状态筛人
        disabled = await client.get("/api/admin/users", params={"status": "disabled"})
        assert [u["login_name"] for u in disabled.json()["data"]["items"]] == ["lisi"]


async def test_member_cannot_probe_email_via_keyword(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """邮箱对 member 不可见，keyword 也不能匹配邮箱，否则可以逐字符试探出来。"""
    a, _ = await _teams(db_session)
    await _directory(db_session, a)
    await login_as(client, db_session, role="member")
    for kw in ("zs@", "example.com"):
        r = await client.get("/api/admin/users", params={"keyword": kw})
        assert r.status_code == 200 and r.json()["data"]["total"] == 0, kw


async def test_keyword_escapes_like_wildcards_and_is_bounded(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await login_as(client, db_session, role="member", login_name="viewer")
    db_session.add_all(
        [
            User(login_name="wang_xin", display_name="王一"),
            User(login_name="wangaxin", display_name="王二"),
            User(login_name="rate", display_name="百分之百%"),
        ]
    )
    await db_session.commit()

    async def names(keyword: str) -> list[str]:
        r = await client.get("/api/admin/users", params={"keyword": keyword})
        assert r.status_code == 200, r.text
        return sorted(u["login_name"] for u in r.json()["data"]["items"])

    assert await names("g_x") == ["wang_xin"]
    assert await names("%") == ["rate"]
    assert await names("\\") == []
    assert (await client.get("/api/admin/users", params={"keyword": "x" * 201})).status_code == 422
    assert (await client.get("/api/admin/users", params={"keyword": "x" * 200})).status_code == 200


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
    # team_lead 的回显同样裁剪：不能借 PATCH 拿到明细
    assert ok.status_code == 200 and ok.json()["data"]["role"] == "team_lead"
    assert set(ok.json()["data"]) == set(PUBLIC_USER_FIELDS)
    await db_session.refresh(target)
    assert target.manual_fields == ["position"] and target.position == "研发"
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

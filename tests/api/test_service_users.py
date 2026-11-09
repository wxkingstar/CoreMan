"""服务用户：只有平台管理角色能建，没有平台身份。"""

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import AuditLog, User, UserIdentity
from tests.api.conftest import login_as

URL = "/api/admin/users/service"
BODY = {"login_name": "svc-demo", "display_name": "演示", "email": "svc-demo@example.test"}


async def test_member_cannot_create(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    await login_as(client, db_session, role="member")
    assert (await client.post(URL, json=BODY)).status_code == 403
    await login_as(client, db_session, role="team_lead")
    assert (await client.post(URL, json=BODY)).status_code == 403


async def test_create_has_no_identity_and_is_audited(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await login_as(client, db_session, role="platform_admin")
    resp = await client.post(URL, json=BODY)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["source"] == "service" and data["status"] == "active"
    user = (
        await db_session.execute(select(User).where(User.login_name == "svc-demo"))
    ).scalar_one()
    idents = await db_session.execute(select(UserIdentity).where(UserIdentity.user_id == user.id))
    assert idents.first() is None
    audit = await db_session.execute(
        select(AuditLog).where(AuditLog.action == "user.service_create")
    )
    assert audit.first() is not None


async def test_duplicate_and_prefix_clash(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await login_as(client, db_session, role="ai_committee")
    assert (await client.post(URL, json=BODY)).status_code == 200
    assert (await client.post(URL, json=BODY)).status_code == 409
    db_session.add(User(display_name="同前缀", email="svc-two@other.test"))
    await db_session.commit()
    clash = {"login_name": "svc-two", "display_name": "x", "email": "svc-two@example.test"}
    assert (await client.post(URL, json=clash)).status_code == 409


async def test_rejects_bad_identity(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    await login_as(client, db_session, role="platform_admin")
    for patch in (
        {"email": "other@example.test"},
        {"email": "svc-demo"},
        {"email": "svc-demo@"},
        {"login_name": "SVC-demo", "email": "SVC-demo@example.test"},
        {"display_name": "  "},
    ):
        resp = await client.post(URL, json={**BODY, **patch})
        assert resp.status_code == 422, patch

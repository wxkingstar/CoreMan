"""管理写操作的审计记录客户端 IP（ASGITransport 的客户端地址是 127.0.0.1）。"""

from sqlalchemy import select

from coreman.core.db.models import AuditLog
from tests.api.test_bot_skills import prepare
from tests.api.test_runtime_nodes import enrollment


async def audit_ips(db_session, prefix: str) -> dict[str, str | None]:
    db_session.expire_all()
    rows = await db_session.scalars(select(AuditLog).where(AuditLog.action.like(f"{prefix}%")))
    return {row.action: str(row.ip).split("/")[0] if row.ip else None for row in rows}


async def test_runtime_node_admin_writes_record_client_ip(client, db_session):
    _, body, _ = await enrollment(client, db_session)
    response = await client.patch(
        f"/api/admin/runtime-nodes/{body['node_id']}", json={"draining": True}
    )
    assert response.status_code == 200, response.text
    link = (
        await client.post(
            "/api/admin/runtime-nodes/install-links", json={"workspace_root": "/home/ai/projects"}
        )
    ).json()["data"]
    response = await client.delete(f"/api/admin/runtime-nodes/install-links/{link['id']}")
    assert response.status_code == 200, response.text
    assert await audit_ips(db_session, "runtime.") == {
        "runtime.install_link.create": "127.0.0.1",
        "runtime.enroll": "127.0.0.1",
        "runtime.update": "127.0.0.1",
        "runtime.install_link.revoke": "127.0.0.1",
    }


async def test_skill_install_request_records_client_ip(client, db_session):
    bot, skill, _ = await prepare(client, db_session)
    response = await client.post(
        f"/api/admin/bots/{bot.id}/skills/{skill.id}/install",
        json={"user_env_vars": {"API_KEY": "synthetic-user-secret"}},
    )
    assert response.status_code == 200, response.text
    assert await audit_ips(db_session, "skill.") == {"skill.install_requested": "127.0.0.1"}

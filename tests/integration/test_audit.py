from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.audit import record_audit
from coreman.core.db.models import AuditLog


async def test_record_audit_persists(db_session: AsyncSession) -> None:
    row = await record_audit(
        db_session,
        action="auth.bootstrap_login",
        actor_login="admin",
        target_type="user",
        target_id="x",
        diff={"role": "platform_admin"},
        ip="127.0.0.1",
    )
    await db_session.commit()
    saved = (await db_session.execute(select(AuditLog))).scalar_one()
    assert saved.id == row.id
    assert saved.action == "auth.bootstrap_login"
    assert saved.diff == {"role": "platform_admin"}
    assert str(saved.ip) == "127.0.0.1"


async def test_record_audit_stores_null_for_invalid_ip(db_session: AsyncSession) -> None:
    """伪造的 X-Forwarded-For 等非法值不能让 INSERT 炸掉：record_audit 自己兜底存 NULL。"""
    await record_audit(db_session, action="user.update", actor_login="admin", ip="not-an-ip")
    await db_session.commit()
    assert (await db_session.execute(select(AuditLog))).scalar_one().ip is None

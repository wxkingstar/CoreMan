from __future__ import annotations

import ipaddress
import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import AuditLog


async def record_audit(
    session: AsyncSession,
    *,
    action: str,
    actor_id: uuid.UUID | None = None,
    actor_login: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    diff: dict[str, Any] | None = None,
    ip: str | None = None,
) -> AuditLog:
    """写一条审计。audit_logs.ip 是 INET 列：伪造的 X-Forwarded-For 等非法值直接写入会让
    INSERT 报错（→500），这里自己兜底存 NULL，调用方不必逐个记得校验。"""
    if ip is not None:
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            ip = None
    row = AuditLog(
        action=action,
        actor_id=actor_id,
        actor_login=actor_login,
        target_type=target_type,
        target_id=target_id,
        diff=diff,
        ip=ip,
    )
    session.add(row)
    await session.flush()
    return row


def diff_dict(
    before: dict[str, Any], after: dict[str, Any], secret_keys: Iterable[str] = ()
) -> dict[str, list[Any]]:
    """只列出变化的键 → [旧, 新]；密钥键的值一律写 ***（审计里不落明文）。"""
    secrets_ = set(secret_keys)
    out: dict[str, list[Any]] = {}
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        out[key] = ["***", "***"] if key in secrets_ else [old, new]
    return out

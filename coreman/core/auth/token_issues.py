"""业务令牌签发记录：业务系统日志里的 token_id（jti）由此查回是哪个 AI 员工、哪轮任务、替谁签的。"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import BusinessTokenIssue

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class IssuedRecord:
    """一次签发的可记录部分：只有令牌标识和元数据，不含令牌本身。"""

    system_key: str
    provider: str
    audience: str
    token_id: str | None
    expires_at: int


async def record_issues(
    session: AsyncSession,
    issued: Iterable[IssuedRecord],
    *,
    purpose: str,
    subject: str,
    task_id: int | None = None,
    bot_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> None:
    """在调用方事务里写签发记录，随调用方一起提交。写失败只记日志：追溯记录不能反过来弄挂任务。"""
    rows = [
        {
            "purpose": purpose,
            "task_id": task_id,
            "bot_id": bot_id,
            "user_id": user_id,
            "subject": subject,
            "system_key": r.system_key,
            "provider": r.provider,
            "audience": r.audience,
            "token_id": r.token_id,
            "expires_at": datetime.fromtimestamp(r.expires_at, UTC),
        }
        for r in issued
    ]
    if not rows:
        return
    try:
        async with session.begin_nested():
            await session.execute(insert(BusinessTokenIssue), rows)
    except Exception as exc:  # noqa: BLE001
        log.warning("business_token_issue_record_failed", task_id=task_id, error=type(exc).__name__)
        return
    # token_id 不是凭据，可以进日志；令牌本身绝不记录。
    log.info(
        "business_tokens_issued",
        purpose=purpose,
        task_id=task_id,
        bot_id=str(bot_id) if bot_id else None,
        subject=subject,
        tokens=[{"system": r["system_key"], "token_id": r["token_id"]} for r in rows],
    )

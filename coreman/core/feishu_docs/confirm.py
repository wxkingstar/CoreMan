"""群里需要提问人确认的写入。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.feishu_docs.policy import DocsScope


async def propose(
    session: AsyncSession,
    scope: DocsScope,
    name: str,
    arguments: dict[str, Any],
    target: str | None,
    digest: str,
) -> dict[str, Any]:
    return {"status": "pending_confirmation"}

"""「我的助理」页的接口：概览、等你出手、子任务、最近落地、设置、停用。

只对所有者本人：要求交互登录会话（机器人令牌 403），助理由登录人推出。读取密文的接口在返回前
记一次访问（`access_log.record`）。待跟进与知识页在 `me_assistant_followups.py`、
`me_assistant_notes.py`。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.api.routers.feishu_personal import interactive_user
from coreman.api.routers.me_assistant_common import DEPENDENCIES, PREFIX
from coreman.core.assistants import owner_view
from coreman.core.db.models import User
from coreman.core.timeutils import utcnow

router = APIRouter(prefix=PREFIX, tags=["my-assistant"], dependencies=DEPENDENCIES)


@router.get("")
async def overview(
    actor: User = Depends(interactive_user), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """概览；没有助理时回空状态与开通指引（不是 404：页面要据此显示怎么开通）。"""
    bot = await owner_view.current(session, actor.id)
    data = (
        await owner_view.absent(session, actor)
        if bot is None
        else await owner_view.overview(session, bot, utcnow())
    )
    # 概览会按默认值建设置行。
    await session.commit()
    return {"code": 0, "data": data}

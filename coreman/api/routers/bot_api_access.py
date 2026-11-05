"""AI 员工的「API 驱动」设置：开关与群别名。创建者和协作者可改。"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, current_user, get_session
from coreman.api.errors import ApiError, forbidden, not_found
from coreman.api.routers.bots import load_bot
from coreman.api.security import verify_csrf
from coreman.core.audit import record_audit
from coreman.core.chat import collaboration_setup as setup
from coreman.core.db.models import Bot, BotConversationAlias, User

router = APIRouter(
    prefix="/api/admin/bots", tags=["api-access"], dependencies=[Depends(verify_csrf)]
)


class ToggleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


async def _bot(session: AsyncSession, user: User, bot_id: uuid.UUID, *, lock: bool = False) -> Bot:
    bot = (
        await session.scalar(select(Bot).where(Bot.id == bot_id).with_for_update())
        if lock
        else await load_bot(session, bot_id)
    )
    if bot is None:
        raise not_found("机器人不存在")
    if not await setup.manageable(session, user, bot):
        raise forbidden()
    if bot.platform != "feishu":
        raise ApiError(422, 422, "目前只有飞书 AI 员工支持 API 驱动")
    return bot


async def _output(session: AsyncSession, bot: Bot) -> dict[str, Any]:
    rows = await session.execute(
        select(BotConversationAlias, User.display_name)
        .outerjoin(User, User.id == BotConversationAlias.created_by)
        .where(BotConversationAlias.bot_id == bot.id)
        .order_by(BotConversationAlias.alias)
    )
    return {
        "enabled": bot.api_access,
        "aliases": [
            {
                "id": str(row.id),
                "alias": row.alias,
                "chat_id": row.chat_id,
                "created_by": name,
                "created_at": row.created_at.isoformat(),
            }
            for row, name in rows.all()
        ],
    }


async def _audit(
    session: AsyncSession, request: Request, user: User, bot: Bot, action: str, diff: dict[str, Any]
) -> None:
    await record_audit(
        session,
        action=action,
        actor_id=user.id,
        actor_login=user.login_name,
        target_type="bot",
        target_id=str(bot.id),
        diff=diff,
        ip=client_ip(request),
    )


@router.get("/{bot_id}/api-access")
async def get_api_access(
    bot_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await _bot(session, user, bot_id)
    return {"code": 0, "data": await _output(session, bot)}


@router.patch("/{bot_id}/api-access")
async def set_api_access(
    bot_id: uuid.UUID,
    body: ToggleIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """关掉后新的调用一律拒绝，排队中的 API 轮次开轮时结掉；正在跑的跑完。"""
    bot = await _bot(session, user, bot_id, lock=True)
    if bot.api_access != body.enabled:
        bot.api_access = body.enabled
        await _audit(session, request, user, bot, "bot.api_access", {"api_access": body.enabled})
        await session.flush()
    data = await _output(session, bot)
    await session.commit()
    return {"code": 0, "data": data}


@router.delete("/{bot_id}/api-aliases/{alias_id}")
async def remove_alias(
    bot_id: uuid.UUID,
    alias_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await _bot(session, user, bot_id, lock=True)
    row = await session.get(BotConversationAlias, alias_id)
    if row is None or row.bot_id != bot.id:
        raise not_found("群别名不存在")
    await session.execute(delete(BotConversationAlias).where(BotConversationAlias.id == row.id))
    await _audit(
        session,
        request,
        user,
        bot,
        "bot.api_alias.unbind",
        {"alias": row.alias, "chat_id": row.chat_id},
    )
    data = await _output(session, bot)
    await session.commit()
    return {"code": 0, "data": data}

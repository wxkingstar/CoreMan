"""消息流卡片的跳转：302 到飞书 AppLink，打开对应会话；带签名时顺带删掉这张卡片。

做法见 core/feishu_open。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.core.bus import outbox
from coreman.core.db.models import Bot
from coreman.core.feishu_open import PATH, applink, chat_biz, verify

router = APIRouter(tags=["feishu-open"])


@router.get(PATH)
async def open_chat(
    request: Request,
    chat: str | None = None,
    app: str | None = None,
    pos: str | None = None,
    b: str | None = None,
    d: str | None = None,
    u: str | None = None,
    s: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> Response:
    target = applink(chat=chat, app=app, position=pos)
    if target is None:
        return PlainTextResponse("invalid link", status_code=400)
    if chat and b and d and u and s:
        await _clear(session, request.app.state.settings.master_key_bytes, chat, b, d, u, s)
    return RedirectResponse(target, status_code=302)


async def _clear(
    session: AsyncSession, key: bytes, chat: str, bot: str, task: str, user: str, sig: str
) -> None:
    """看过了：交给这个机器人的飞书网关删掉卡片。签名不对、机器人不在了就只跳转。"""
    try:
        bot_id = uuid.UUID(bot)
    except ValueError:
        return
    if (
        not verify(key, str(bot_id), chat, task, user, sig)
        or await session.get(Bot, bot_id) is None
    ):
        return
    await outbox.add(
        session,
        bot_id=bot_id,
        platform="feishu",
        kind="send",
        dedupe_key=f"feed_click:{task}:{user}",
        target={"user_id": user},
        payload={"_feed_delete": {"biz": chat_biz(chat), "user": user}},
    )
    await session.commit()

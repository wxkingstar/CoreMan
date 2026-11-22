"""消息流卡片的跳转：302 到飞书 AppLink，打开对应会话。做法见 core/feishu_open。"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

from coreman.core.feishu_open import PATH, applink

router = APIRouter(tags=["feishu-open"])


@router.get(PATH)
async def open_chat(chat: str | None = None, app: str | None = None) -> Response:
    target = applink(chat=chat, app=app)
    if target is None:
        return PlainTextResponse("invalid link", status_code=400)
    return RedirectResponse(target, status_code=302)

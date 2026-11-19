"""飞书链接预览回调（url.preview.get）：校验令牌与应用，3 秒内回一行说明。

做法见 core/feishu_link_preview。

没配齐（应用、Verification Token）时一律回空对象：飞书照原样显示链接，不报错。
"""

from __future__ import annotations

import hmac
import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.core.feishu_link_preview import EVENT_TYPE, PATH, PreviewError, decrypt, preview
from coreman.core.logging import get_logger

router = APIRouter(tags=["feishu-link-preview"])
log = get_logger(__name__)
MAX_BODY = 65_536


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


@router.post(PATH)
async def link_preview(request: Request, session: AsyncSession = Depends(get_session)) -> Any:
    cfg = request.app.state.settings
    token = cfg.feishu_preview_verification_token
    if not cfg.feishu_preview_app_id or token is None or not token.get_secret_value():
        return {}
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return JSONResponse({"error": "too_large"}, status_code=413)
    try:
        body = json.loads(raw)
        if isinstance(body, dict) and "encrypt" in body:
            key = cfg.feishu_preview_encrypt_key
            if key is None or not key.get_secret_value():
                raise PreviewError("no_key")
            body = decrypt(key.get_secret_value(), str(body["encrypt"]))
    except (ValueError, PreviewError):
        return JSONResponse({"error": "bad_request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "bad_request"}, status_code=400)
    expected = token.get_secret_value()
    if body.get("type") == "url_verification":
        # 开发者后台配置回调地址时的校验。
        if not hmac.compare_digest(str(body.get("token") or ""), expected):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return {"challenge": body.get("challenge")}
    header = _dict(body.get("header"))
    if (
        not hmac.compare_digest(str(header.get("token") or ""), expected)
        or header.get("app_id") != cfg.feishu_preview_app_id
    ):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if header.get("event_type") != EVENT_TYPE:
        return {}
    event = _dict(body.get("event"))
    context, operator = _dict(event.get("context")), _dict(event.get("operator"))
    url = context.get("url")
    if not isinstance(url, str) or len(url) > 2048:
        return {}
    return await preview(
        session,
        base_url=cfg.public_base_url,
        url=url,
        operator_user_id=str(operator.get("user_id") or ""),
    )

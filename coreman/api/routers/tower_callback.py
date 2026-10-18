"""Tower 决定回调：验签、去重、更新挂起单，按决定续接原对话。

Tower 侧按 2xx 判定送达、非 2xx 退避重试，所以只有验签失败与报文畸形才返回非 2xx；
重放、不是代理发起的审批、认不出的状态都回 200，免得 Tower 白白重试 24 小时。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.core.cloud_proxy import decisions, resume
from coreman.core.db.models import AuthNonce
from coreman.core.logging import get_logger
from coreman.core.timeutils import parse_ts

router = APIRouter(tags=["tower-callback"])
log = get_logger(__name__)
PATH = "/api/integrations/tower/callback"
MAX_BODY = 65_536
WINDOW_SECONDS = 300
NONCE_KIND = "tower_callback"


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b"\n" + body, hashlib.sha256).hexdigest()


@router.post(PATH)
async def tower_callback(request: Request, session: AsyncSession = Depends(get_session)) -> Any:
    secret = request.app.state.settings.tower_callback_secret
    if secret is None or not secret.get_secret_value():
        return _error(503, "callback_disabled")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY:
            return _error(413, "body_too_large")
    body = bytes(raw)
    ts = request.headers.get("X-Tower-Timestamp", "")
    if not (ts.isascii() and ts.isdecimal() and len(ts) <= 12):
        return _error(401, "invalid_signature")
    # 签名覆盖时间戳：时间窗挡住旧报文，去重挡住窗内重放
    if abs(time.time() - int(ts)) > WINDOW_SECONDS:
        return _error(401, "stale_timestamp")
    given = request.headers.get("X-Tower-Signature", "")
    expected = sign(secret.get_secret_value(), ts, body)
    if not hmac.compare_digest(expected.encode(), given.encode()):
        return _error(401, "invalid_signature")
    try:
        data = json.loads(body)
    except ValueError:
        return _error(400, "bad_body")
    if not isinstance(data, dict):
        return _error(400, "bad_body")
    approval_id, external_id, status = (
        data.get("approval_id"),
        data.get("external_id"),
        data.get("status"),
    )
    if (
        not isinstance(approval_id, str)
        or not decisions.APPROVAL_ID.fullmatch(approval_id)
        or not isinstance(external_id, str)
        or not external_id
        or len(external_id) > 256
    ):
        # 签名对了但字段畸形：是 Tower 那边的问题，留一行日志便于对账
        log.warning("tower_callback_malformed")
        return _error(400, "bad_body")
    if status not in resume.RESUME_STATUSES:
        # pending、withdrawn（发起方自己撤的，Tower 不该回调）或以后新增的状态：签名可信但无事可做
        log.info("tower_callback_ignored_status", approval_id=approval_id)
        return {"ignored": True}
    # 去重只防时间窗内的重放：auth_wecom 的清理会把一天前的 AuthNonce 一并删掉，
    # 长期的幂等靠挂起单只从 pending 往前走
    fresh = await session.scalar(
        insert(AuthNonce)
        .values(kind=NONCE_KIND, value=f"{approval_id}:{status}")
        .on_conflict_do_nothing(index_elements=[AuthNonce.kind, AuthNonce.value])
        .returning(AuthNonce.value)
    )
    if not fresh:
        # 第一次已处理、只是应答丢了：照样回 2xx，否则 Tower 会把送达过的回调记成失败
        return {"duplicate": True}
    reason = data.get("reason")
    outcome = await decisions.apply(
        session,
        approval_id=approval_id,
        external_id=external_id,
        status=status,
        reason=reason if isinstance(reason, str) else None,
        decided_at=parse_ts(data.get("decided_at")),
    )
    if not outcome.known:
        # 别的发送端流程或非代理发起的审批：记下即可，挂起单不归它管
        log.info("tower_callback_ignored", approval_id=approval_id)
        await session.commit()
        return {"ignored": True}
    await session.commit()
    return {"ok": True, "resumed": outcome.resumed}

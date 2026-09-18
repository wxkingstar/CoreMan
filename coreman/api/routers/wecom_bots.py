"""扫码创建企业微信智能机器人。凭证只在服务端流转，浏览器只拿到状态与二维码内容。"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.bot_permissions import can_create_bot
from coreman.api.deps import client_ip, current_user, get_session, get_store
from coreman.api.errors import ApiError, forbidden
from coreman.api.security import verify_csrf
from coreman.core.audit import record_audit
from coreman.core.crypto import Cipher
from coreman.core.db.models import User
from coreman.core.settings_schema import SETTING_DEFAULTS
from coreman.core.settings_store import SettingsStore
from coreman.core.wecom_bots import service as provisions

router = APIRouter(prefix="/api/admin", tags=["wecom-bots"], dependencies=[Depends(verify_csrf)])
FLAG = "wecom_qr_provisioning_enabled"


def _cipher(request: Request) -> Cipher:
    return request.app.state.cipher  # type: ignore[no-any-return]


class ProvisionIn(BaseModel):
    # 优先复用本人扫码创建、还没被员工使用的机器人。
    reuse: bool = True


@router.post("/wecom-bot-provisions", status_code=201)
async def start_provision(
    body: ProvisionIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    store: SettingsStore = Depends(get_store),
) -> dict[str, Any]:
    if not can_create_bot(user):
        raise forbidden()
    cipher = _cipher(request)
    # 已经扫码建好的机器人不受开关影响：关掉扫码通道后也不该让它白白过期。
    if body.reuse and (row := await provisions.reusable(session, user)):
        return {"code": 0, "data": {**provisions.out(cipher, row), "reused": True}}
    if not await store.get(FLAG, default=SETTING_DEFAULTS[FLAG]):
        raise ApiError(409, 409, "扫码创建企业微信机器人已关闭，请手动填写 Bot ID 与 Secret")
    row = await provisions.start(session, cipher, user)
    await record_audit(
        session,
        action="wecom_bot.provision_start",
        actor_id=user.id,
        actor_login=user.login_name,
        target_type="wecom_bot_provision",
        target_id=str(row.id),
        diff={"status": [None, row.status]},
        ip=client_ip(request),
    )
    # 生成失败也要落库：计入限流，告警规则也靠它发现接口改版。
    await session.commit()
    if row.status == "failed":
        raise ApiError(
            502, 502, "企业微信暂时无法生成二维码，请稍后重试，或改为手动填写 Bot ID 与 Secret"
        )
    return {"code": 0, "data": {**provisions.out(cipher, row), "reused": False}}


@router.get("/wecom-bot-provisions/{provision_id}")
async def get_provision(
    provision_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    cipher = _cipher(request)
    row = await provisions.load(session, user, provision_id)
    retry_after = await provisions.refresh(cipher, row)
    await session.commit()
    return {"code": 0, "data": provisions.out(cipher, row, retry_after)}


@router.delete("/wecom-bot-provisions/{provision_id}")
async def cancel_provision(
    provision_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    cipher = _cipher(request)
    row = await provisions.load(session, user, provision_id)
    if row.status == "pending":
        # 关窗口前可能刚扫完码：先查最后一次，建好了就留着下次复用，免得机器人没人接管。
        row.next_poll_at = None
        await provisions.refresh(cipher, row)
    if row.status == "pending":
        provisions.cancel(row)
    await session.commit()
    return {"code": 0, "data": provisions.out(cipher, row)}

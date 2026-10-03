"""飞书个人凭证表单的提交：解封网关封存的值、交给 service.submit()，并擦掉封存副本。

调用方（CardActionHandler）已经证明这张卡是我们发到这个会话的；这里只认点击人是不是发起人。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.chat.identity import resolve_speaker
from coreman.core.crypto import DecryptError
from coreman.core.db.models import Bot, InboundEvent
from coreman.core.personal_credentials import policy, service
from coreman.core.personal_credentials.policy import CredentialError
from coreman.runtime.worker.context import TaskContext

_ERRORS = {"forbidden": "not_owner", "not_found": "ignored"}


async def wipe(session: AsyncSession, task_id: int, event_id: int | None) -> None:
    """封存副本用完即删：任务与入站事件按 90 天保留，不留这份密文。"""
    await session.execute(
        text("UPDATE tasks SET payload = payload #- '{card_action,sealed}' WHERE id = :id"),
        {"id": task_id},
    )
    if event_id is not None:
        await session.execute(
            text(
                "UPDATE inbound_events SET payload = payload #- '{card_action,sealed}' "
                "WHERE id = :id"
            ),
            {"id": event_id},
        )


async def wipe_detached(ctx: TaskContext) -> None:
    """兜底：另开会话和事务擦本任务的封存副本。

    主流程里的擦除与提交同一事务，处理器一抛异常这个事务连它一起回滚，副本就会留在失败的
    任务和入站事件里。所以不管怎么退出，最后都在全新的事务里再擦一遍；这一步自己出错只记日志，
    不盖掉原来的异常。
    """
    try:
        async with ctx.session_factory() as session:
            await wipe(session, ctx.task.id, ctx.task.inbound_event_id)
            await session.commit()
    except Exception:  # noqa: BLE001 擦不掉也不能盖住处理器原本的异常
        ctx.log.exception("credential_wipe_failed")


async def _submit(
    session: AsyncSession, ctx: TaskContext, inbound: InboundEvent, action: dict[str, Any]
) -> str:
    request_id = policy.parse_card_task_id(str(action.get("task_id") or ""))
    sealed = action.get("sealed")
    if request_id is None or not isinstance(sealed, str):
        return "ignored"
    speaker = await resolve_speaker(
        session, platform="feishu", platform_user_id=inbound.sender_platform_user_id or ""
    )
    if speaker.user_id is None:
        return "not_owner"
    try:
        values = json.loads(ctx.cipher.decrypt(sealed, policy.sealed_aad(request_id)))
    except (DecryptError, ValueError):
        return "ignored"
    try:
        result = await service.submit(
            session,
            ctx.cipher,
            request_id,
            actor_id=speaker.user_id,
            values=values,
            limit=policy.FEISHU_MAX_VALUE,
        )
    except CredentialError as exc:
        return _ERRORS.get(exc.code, exc.code)
    if result.status == "invalid":
        # 卡片上的表单不能原地改错：取消这次请求，agent 再发起会得到一张新表单。
        await service.cancel(session, request_id, result.message)
    return result.status


async def handle(
    session: AsyncSession,
    ctx: TaskContext,
    bot: Bot,
    inbound: InboundEvent,
    action: dict[str, Any],
) -> str:
    result = await _submit(session, ctx, inbound, action)
    # 与 submit 同一事务：提交成功时副本一并消失。抛异常的路径由调用方的 wipe_detached 兜底。
    await wipe(session, ctx.task.id, inbound.id)
    return result

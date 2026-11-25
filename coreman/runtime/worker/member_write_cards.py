"""Clicks on the confirmation card of a write a member's own agent staged (`member_writes`).

The caller (`card_actions`) proved the card was sent by us to the clicker's private chat with the
public bot. Only the member who staged the write counts. On confirm the write is checked again
(member, OAuth grant, system, operation definition and visibility) and then run once, exactly as
staged, as the member. The outcome is kept for `systems_call_result` and shown on the card.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth.system_access import SubjectUnavailable
from coreman.core.bus import outbox, tasks
from coreman.core.db.models import Bot, InboundEvent, InteractionState, User, UserIdentity
from coreman.core.systems_catalog import confirm, member_writes, proxy, service
from coreman.core.systems_catalog.context import Context
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.context import TaskContext


async def _clicker(session: AsyncSession, inbound: InboundEvent) -> uuid.UUID | None:
    pid = str(inbound.sender_platform_user_id or "")
    if not pid:
        return None
    user_id: uuid.UUID | None = await session.scalar(
        select(UserIdentity.user_id).where(
            UserIdentity.platform == "feishu", UserIdentity.platform_user_id == pid
        )
    )
    return user_id


async def _update(
    session: AsyncSession,
    bot: Bot,
    inbound: InboundEvent,
    text: str,
    *,
    title: str = "没有执行",
    template: str = "grey",
) -> None:
    await outbox.add(
        session,
        bot_id=bot.id,
        platform="feishu",
        kind="card_update",
        dedupe_key=f"{inbound.id}:card_update",
        target={"chat_id": inbound.chat_id, "message_id": inbound.reply_context.get("message_id")},
        payload={"card": confirm.result_card(title, template, text)},
    )


async def _context(
    session: AsyncSession, ctx: TaskContext, state: InteractionState
) -> Context | None:
    user = await session.get(User, uuid.UUID(str(state.state["user_id"])))
    grant = await member_writes.active_grant(session, state)
    if user is None or user.status != "active" or grant is None:
        return None
    issuance = service.Issuance(
        cipher=ctx.cipher,
        issuer=str(await ctx.settings_store.get("jwt_issuer", default="coreman")),
        external_key=ctx.external_jwt_key,
        providers=ctx.business_token_providers,
    )
    try:
        return await member_writes.build_context(session, issuance, user, grant)
    except SubjectUnavailable:
        return None


async def _settle(
    session: AsyncSession,
    bot: Bot,
    inbound: InboundEvent,
    state: InteractionState,
    outcome: dict[str, Any],
    card_text: str,
    *,
    title: str = "没有执行",
    template: str = "grey",
) -> None:
    state.state = {**state.state, "result": outcome}
    await _update(session, bot, inbound, card_text, title=title, template=template)


async def handle(
    session: AsyncSession,
    ctx: TaskContext,
    bot: Bot,
    inbound: InboundEvent,
    action: dict[str, Any],
) -> str:
    verb, _, raw_id = str(action.get("level") or "").partition(":")
    if verb not in ("confirm", "cancel"):
        return "ignored"
    try:
        state_id = uuid.UUID(raw_id)
    except ValueError:
        return "ignored"
    if (
        ctx.task.kind != "card_action"
        or ctx.task.status not in tasks.ACTIVE
        or ctx.task.cancel_requested_at
        or inbound.platform != "feishu"
        or inbound.kind != "card_action"
        or inbound.bot_id != bot.id
        or action.get("task_id") != member_writes.card_ref(state_id)
    ):
        return "ignored"
    state = await session.scalar(
        select(InteractionState)
        .where(
            InteractionState.id == state_id,
            InteractionState.kind == member_writes.KIND,
            InteractionState.bot_id == bot.id,
        )
        .with_for_update()
    )
    if state is None or not state.state.get("sent"):
        return "ignored"
    who = await _clicker(session, inbound)
    if (
        who is None
        or str(who) != state.state.get("user_id")
        or str(inbound.sender_platform_user_id or "") != state.state.get("platform_user_id")
    ):
        return "ignored"
    now = utcnow()
    if state.status != "open":
        return "ignored"
    if state.expires_at is not None and state.expires_at <= now:
        await confirm.expire(
            session,
            state,
            now,
            dedupe_key=f"{inbound.id}:card_update",
            chat_id=inbound.chat_id,
            message_id=inbound.reply_context.get("message_id"),
        )
        return "expired"
    if verb == "cancel":
        state.status = "cancelled"
        await _update(session, bot, inbound, "已取消，没有执行。")
        return "member_write_cancelled"
    # 先落库再调业务系统：中途崩溃、任务重试都不会再执行一遍。
    state.status = "submitted"
    state.state = {**state.state, "sending_at": now.isoformat()}
    await session.commit()
    [item] = member_writes.unseal(ctx.cipher, state)
    pctx = await _context(session, ctx, state)
    if pctx is None:
        await _settle(
            session,
            bot,
            inbound,
            state,
            {"status": "refused", "message": "确认时已无法以你的身份调用业务系统，没有执行。"},
            "确认时已无法以你的身份调用业务系统（账号停用、本机授权已撤销或身份不全），没有执行。",
        )
        return "member_write_unavailable"
    p = await proxy.prepare(
        pctx,
        proxy.Call(
            system=item["system"],
            operation_id=item["operation_id"],
            path_params=item.get("path_params"),
            query=item.get("query"),
            headers=item.get("headers"),
            body=item.get("body"),
        ),
    )
    if isinstance(p, dict):
        reason: str | None = str(p.get("message") or p.get("error") or "检查未通过")
    elif p.op["risk"] not in proxy.CONFIRMED_RISKS or confirm.op_digest(p.op) != item["op_digest"]:
        reason = "业务系统的操作定义已经变化"
    else:
        reason = None
    if isinstance(p, dict) or reason is not None:
        await _settle(
            session,
            bot,
            inbound,
            state,
            {
                "status": "refused",
                "message": f"确认时已不能执行：{reason}。没有执行，不要自行重试。",
            },
            f"确认时已不能执行（{reason}），没有执行。",
        )
        return "member_write_refused"
    try:
        result = await proxy.send(pctx, p, key=item["key"], prefix="confirmed_")
    except Exception as exc:  # noqa: BLE001 平台自己出错：请求可能已经发出
        await session.rollback()
        # 回滚让已加载的行都过期：重新取，后面记结果、改卡片还要用。
        for row in (state, bot, inbound):
            await session.refresh(row)
        result = {
            "error": "platform_error",
            "maybe_sent": True,
            "message": f"平台处理时出错（{type(exc).__name__}），不确定请求有没有发出。",
        }
    status = "ok" if result.get("ok") else "unknown" if result.get("maybe_sent") else "failed"
    mark = confirm.MARKS[status]
    headline = confirm.headline(1, item, numbered=False)
    tail = (
        ""
        if status == "ok"
        else "\n结果未知，可能已经生效，请先到业务系统核对。"
        if status == "unknown"
        else "\n没有成功，本机的 Agent 会接着说明原因。"
    )
    await _settle(
        session,
        bot,
        inbound,
        state,
        {
            "status": status,
            "response": member_writes.brief(result),
            "message": "已按确认内容执行。读回关键字段核对后如实汇报。"
            if status == "ok"
            else "结果未知，可能已经生效：先用读操作核对实际状态，不要直接重做。"
            if status == "unknown"
            else "没有成功：把原因告诉用户，不要自行重试。",
        },
        f"{mark[0]} {headline}：{mark[1]}{tail}",
        title="已完成" if status == "ok" else "没有成功" if status == "failed" else "结果未知",
        template="green" if status == "ok" else "red" if status == "failed" else "orange",
    )
    return f"member_write_{status}"

"""Clicks on the business-system write confirmation card.

Only the asker of the turn that staged the writes counts, in the chat the card was sent to. On
confirm every staged write is checked again against today's grants and catalog; if any no longer
passes, nothing runs. Then they run in order as the asker, stopping at the first failure, and the
conversation continues with the results.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth.system_access import SubjectUnavailable, granted_systems, token_subject
from coreman.core.bus import outbox, tasks
from coreman.core.db.models import Bot, BotSystemGrant, InboundEvent, InteractionState, Task, User
from coreman.core.systems_catalog import confirm, proxy, service
from coreman.core.systems_catalog.context import Context
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.personal_cards import card_owner_scope, group_card_scope

RESULT_MAX = 1500


async def _clicker(
    session: AsyncSession, origin: Task, inbound: InboundEvent, chat_type: str
) -> tuple[uuid.UUID, str] | None:
    """(user_id, chat_id) of a click made by the asker of `origin` in its chat."""
    try:
        if chat_type == "group":
            group = await group_card_scope(session, origin, inbound)
            return (group.user_id, group.chat_id) if group else None
        scope = await card_owner_scope(session, origin, inbound)
        return (scope.user_id, scope.chat_id) if scope else None
    except (ValueError, KeyError, TypeError):
        return None


async def _context(
    session: AsyncSession, ctx: TaskContext, bot: Bot, origin: Task, user: User
) -> Context | None:
    """The proxy context of the asker, rebuilt from today's grants."""
    try:
        subject = await token_subject(session, user)
    except SubjectUnavailable:
        return None
    writable = await session.scalars(
        select(BotSystemGrant.system_key).where(
            BotSystemGrant.bot_id == bot.id, BotSystemGrant.allow_write
        )
    )
    return Context(
        session=session,
        issuance=service.Issuance(
            cipher=ctx.cipher,
            issuer=str(await ctx.settings_store.get("jwt_issuer", default="coreman")),
            external_key=ctx.external_jwt_key,
            providers=ctx.business_token_providers,
        ),
        operator=service.Operator(user=user, subject=subject, task_id=origin.id, bot_id=bot.id),
        systems=await granted_systems(session, bot.id),
        writable=frozenset(writable),
        token_ttl=bot.sse_timeout_seconds,
    )


async def _recheck(
    pctx: Context, items: list[dict[str, Any]]
) -> list[proxy.Prepared] | tuple[int, str]:
    """Every staged item, prepared again; (index, reason) of the first that no longer passes."""
    prepared: list[proxy.Prepared] = []
    for index, item in enumerate(items, 1):
        args = proxy.Call(
            system=item["system"],
            operation_id=item["operation_id"],
            path_params=item.get("path_params"),
            query=item.get("query"),
            body=item.get("body"),
        )
        p = await proxy.prepare(pctx, args)
        if isinstance(p, dict):
            return index, str(p.get("message") or p.get("error") or "检查未通过")
        changed = confirm.op_digest(p.op) != item["op_digest"]
        if p.op["risk"] not in proxy.CONFIRMED_RISKS or changed:
            return index, "业务系统的操作定义已经变化"
        prepared.append(p)
    return prepared


def _brief(result: dict[str, Any]) -> str:
    """One item's outcome for the model: status and a bounded response."""
    data = {k: v for k, v in result.items() if k not in ("system", "operation_id", "content_trust")}
    text = json.dumps(data, ensure_ascii=False, default=str)
    return text[:RESULT_MAX] + ("…" if len(text) > RESULT_MAX else "")


async def _run(
    pctx: Context, items: list[dict[str, Any]], prepared: list[proxy.Prepared]
) -> tuple[list[str], list[str], bool]:
    """Run in order, stop at the first failure. (card lines, model notes, all succeeded)."""
    lines: list[str] = []
    notes: list[str] = []
    stopped = False
    numbered = len(items) > 1
    for index, (item, p) in enumerate(zip(items, prepared, strict=True), 1):
        head = confirm.headline(index, item, numbered=numbered)
        if stopped:
            lines.append(f"⏸ {head}：没有执行")
            notes.append(f"{index}. {item['system']}.{item['operation_id']}：未执行")
            continue
        result = await proxy.send(pctx, p, key=item["key"], prefix="confirmed_")
        ok = bool(result.get("ok"))
        lines.append(f"✅ {head}：已完成" if ok else f"❌ {head}：没有成功")
        notes.append(
            f"{index}. {item['system']}.{item['operation_id']}："
            + ("成功" if ok else "失败")
            + " "
            + _brief(result)
        )
        stopped = not ok
    return lines, notes, not stopped


async def handle(
    session: AsyncSession,
    ctx: TaskContext,
    bot: Bot,
    inbound: InboundEvent,
    action: dict[str, Any],
) -> str:
    """The caller proved the card was sent by us to this chat. Staged values stay out of logs."""
    verb, _, raw_id = str(action.get("level") or "").partition(":")
    if verb not in ("confirm", "cancel"):
        return "ignored"
    try:
        origin = await session.get(Task, int(str(action["task_id"]).split(":")[1]))
        state_id = uuid.UUID(raw_id)
    except (KeyError, ValueError, IndexError):
        return "ignored"
    if (
        origin is None
        or origin.bot_id != bot.id
        or ctx.task.kind != "card_action"
        or ctx.task.status not in tasks.ACTIVE
        or ctx.task.cancel_requested_at
        or inbound.platform != "feishu"
        or inbound.kind != "card_action"
        or inbound.bot_id != bot.id
    ):
        return "ignored"
    state = await session.scalar(
        select(InteractionState)
        .where(
            InteractionState.id == state_id,
            InteractionState.kind == confirm.KIND,
            InteractionState.bot_id == bot.id,
        )
        .with_for_update()
    )
    staged = state.state if state else {}
    if state is None or staged.get("origin_task_id") != origin.id or not staged.get("sent"):
        return "ignored"
    who = await _clicker(session, origin, inbound, str(staged.get("chat_type")))
    if who is None or str(who[0]) != staged.get("user_id") or who[1] != staged.get("chat_id"):
        return "ignored"
    if state.status != "open":
        return "expired"
    if state.expires_at is not None and state.expires_at <= utcnow():
        state.status = "expired"
        await _update(session, bot, inbound, "这张确认卡片已过期，没有执行。")
        return "expired"
    items = confirm.unseal(ctx.cipher, origin.id, staged)
    if verb == "cancel":
        state.status = "cancelled"
        await _update(session, bot, inbound, "已取消，没有执行。")
        await _resume(session, origin, state, "用户取消了这批写操作，都没有执行。不要重试。")
        return "systems_write_cancelled"
    # 先落库再调业务系统：中途崩溃、任务重试都不会再执行一遍。
    state.status = "submitted"
    await session.commit()
    user = await session.get(User, who[0])
    pctx = await _context(session, ctx, bot, origin, user) if user is not None else None
    if pctx is None:
        await _update(session, bot, inbound, "无法以你的身份调用业务系统，没有执行。")
        await _resume(session, origin, state, "确认后无法以提问人身份调用业务系统，都没有执行。")
        return "systems_write_unavailable"
    checked = await _recheck(pctx, items)
    if isinstance(checked, tuple):
        index, reason = checked
        await _update(
            session, bot, inbound, f"第 {index} 项已不能执行（{reason}），整批都没有执行。"
        )
        await _resume(
            session,
            origin,
            state,
            f"确认时第 {index} 项已不能执行：{reason}。整批都没有执行，不要自行重试。",
        )
        return "systems_write_refused"
    lines, notes, all_ok = await _run(pctx, items, checked)
    tail = "" if all_ok else "\n遇到失败已停下，AI 员工会接着说明原因。"
    await _update(
        session,
        bot,
        inbound,
        "\n".join(lines) + tail,
        title="已完成" if all_ok else "没有全部完成",
        template="green" if all_ok else "red",
    )
    await _resume(
        session,
        origin,
        state,
        "用户已确认，平台按确认内容执行的结果：\n"
        + "\n".join(notes)
        + (
            "\n读回关键字段核对后如实汇报。"
            if all_ok
            else "\n遇到失败已停止，失败和未执行的项不要自行重试，先告诉用户。"
        ),
    )
    return "systems_write_done" if all_ok else "systems_write_failed"


async def _resume(session: AsyncSession, origin: Task, state: InteractionState, note: str) -> None:
    await tasks.enqueue(
        session,
        tasks.NewTask(
            bot_id=origin.bot_id,
            kind="chat",
            user_id=origin.user_id,
            session_key=origin.session_key,
            inbound_event_id=origin.inbound_event_id,
            dedupe_key=f"systems-write:{state.id}:resume",
            payload={
                **{k: v for k, v in origin.payload.items() if k != confirm.RESULT_KEY},
                confirm.RESULT_KEY: note,
                "serialize_session": True,
                # 用户刚点完确认卡，不再问「还要不要做」。
                "stale_confirmed": True,
            },
        ),
    )


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
        payload={
            "card": {
                "schema": "2.0",
                "header": {
                    "template": template,
                    "title": {"tag": "plain_text", "content": title},
                },
                # 结果里可能有业务数据：纯文本，不让其中的标记生效。
                "body": {
                    "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": text}}]
                },
            }
        },
    )

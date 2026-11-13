"""调度公共员工的审批单：要对方改东西（`allow_write`）时先由所有者在审批系统里批准。

复用审批闸门的表、回调与对账（`assistant_approvals`、`service.decide`）：这张单的工具名是
`assistant_ask_bot`、目标系统是 `assistant-dispatch`，参数是目标员工与问题原文。提交时不占
求助的名额；批准后的执行任务核销令牌、重查资格、派出，行的编号沿用审批单的编号，所以重跑
不会派第二次。批准并派出之后不单独投一轮（答复由结果回合带回）；被拒、过期、派不出去照旧
投回助理会话。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.assistants import dispatch, jobs, sealed
from coreman.core.assistants.approvals import execute, service, tower
from coreman.core.db.models import AssistantApproval, AssistantJob, Bot
from coreman.core.feishu_personal.policy import Scope
from coreman.core.logging import get_logger
from coreman.core.timeutils import utcnow

log = get_logger(__name__)
# `assistant_approvals.channel` 的现有取值；这类单靠工具名区分。
CHANNEL = "other"
LABEL = "调度员工"
BAR = "│ "
NOTICE = (
    "说明：对方以你的身份、用你在它那里的业务系统权限执行，平台无法限定它具体改什么。"
    "问题与它的答复会留在这位员工的记录和它所在的节点上，只有你本人看得到内容。"
)


def _name(target: Bot) -> str:
    """员工名压成一行：名字里夹带换行也伪造不出审批单的抬头行。"""
    return " ".join(target.name.split()) or target.bot_key


def abstract(target: Bot) -> str:
    """单行摘要，会出现在锁屏推送里：只有渠道和对象。"""
    text = f"{LABEL} → {_name(target)}"
    return text if len(text) <= 120 else text[:119] + "…"


def render(target: Bot, question: str) -> str:
    """审批单正文（纯文本）：对象、动作、问题原文逐行加竖线。"""
    quoted = "\n".join(BAR + line for line in (question.splitlines() or [""]))
    return (
        f"渠道：{LABEL}\n"
        f"对象：{_name(target)}（{target.bot_key}）\n"
        "动作：请这位 AI 员工处理下面的事，并允许它改动数据\n\n"
        f"将要交给它的问题：\n{quoted}\n\n{NOTICE}"
    )


def _calls(target: Bot, question: str, *, unattended: bool) -> list[dict[str, Any]]:
    """进 `args_enc` 的参数，形状与写类个人工具的相同（请求体与去重都只看 `arguments`）。"""
    return [
        {
            "arguments": {
                "bot_id": str(target.id),
                "bot": target.bot_key,
                "name": _name(target),
                "question": question,
            },
            "chat_id": None,
            "anchor_message_id": None,
            "thread_id": None,
            # 发起它的那一轮所有者在不在场：派出时写进审计。
            "unattended": unattended,
        }
    ]


async def submit(
    session: AsyncSession,
    scope: Scope,
    *,
    target: Bot,
    question: str,
    cfg: tower.Config | None,
) -> dict[str, Any]:
    """提交一张调度单；不派出。调用方已核对资格与问题长度，并负责提交事务。

    Args:
        cfg: `tower.config(settings, tower.DISPATCH_TARGET)`；None 表示部署没登记这个目标系统

    Returns:
        `{"status": "pending_approval", "approval_id": …}`；同一位员工、同一个问题已有在途的单
        时返回那一张（带 `duplicate`）；不能提交时是 `{"error": …, "hint": …}`
    """
    requester = await service._requester(session, scope, cfg)
    if isinstance(requester, dict):
        return requester
    assert cfg is not None
    bot = scope.bot
    calls = _calls(target, question, unattended=scope.unattended)
    try:
        digest = await sealed.content_mac(
            session, bot.id, service.HASH_PURPOSE, service._dedupe_text(dispatch.TOOL, calls)
        )
    except sealed.ContentLocked:
        return {"error": sealed.LOCKED_CODE}
    existing: AssistantApproval | None = await session.scalar(
        select(AssistantApproval)
        .where(
            AssistantApproval.bot_id == bot.id,
            AssistantApproval.args_hash == digest,
            AssistantApproval.status.in_(service.LIVE),
        )
        .limit(1)
    )
    if existing is not None:
        return {"status": "pending_approval", "approval_id": str(existing.id), "duplicate": True}
    pending = await session.scalar(
        select(func.count())
        .select_from(AssistantApproval)
        .where(AssistantApproval.bot_id == bot.id, AssistantApproval.status == "pending")
    )
    if (pending or 0) >= service.MAX_PENDING:
        return {
            "error": "too_many_pending",
            "limit": service.MAX_PENDING,
            "hint": "同时待批的审批单已到上限：等本人处理，或用 approval_withdraw 撤回不再需要的。",
        }
    row_id = uuid.uuid4()
    external_id = service.EXTERNAL_PREFIX + row_id.hex
    summary, body = abstract(target), tower.request_body(dispatch.TOOL, calls)
    try:
        # 审批系统没收下就连同暂存的行一起回滚：不留一张谁也批不了的单。
        async with session.begin_nested():
            row = AssistantApproval(
                id=row_id,
                bot_id=bot.id,
                tool=dispatch.TOOL,
                external_id=external_id,
                origin_event_id=scope.event.id if scope.event is not None else None,
                session_key=scope.task.session_key or "",
                args_enc={"calls": calls, "abstract": summary, "background": "", "research": ""},
                args_hash=digest,
                request_hash=tower.request_hash(cfg, external_id, body, bot.bot_key),
                channel=CHANNEL,
                expires_at=utcnow() + service.TTL,
            )
            session.add(row)
            await session.flush()
            sent = await tower.submit(
                cfg,
                external_id=external_id,
                requester=requester,
                caller=bot.bot_key,
                abstract=summary,
                content=render(target, question),
                body=body,
                expires_at=row.expires_at,
            )
            row.approval_id = sent.approval_id
    except tower.TowerError as exc:
        log.warning("assistant_dispatch_submit_failed", approval=str(row_id), code=exc.code)
        return service._tower_failure(exc)
    return {
        "status": "pending_approval",
        "approval_id": str(row_id),
        "hint": service.PENDING_HINT,
    }


async def run(
    factory: async_sessionmaker[AsyncSession],
    approval_id: uuid.UUID,
    cfg: tower.Config | None,
) -> str:
    """执行一张已批准的调度单：核销令牌，重查资格，派出。返回审批单最后的状态。

    两段事务：先核销并记下「开始执行」，再派出。第二段没做完进程就没了，重跑时不再核销，按
    编号看有没有已经派出的行。

    Raises:
        execute.Retry: 取令牌或核销时审批系统不可用；审批单保持 approved，什么都没做
    """
    async with factory() as session:
        row = await session.get(
            AssistantApproval, approval_id, with_for_update=True, populate_existing=True
        )
        if row is None or row.status != "approved":
            return row.status if row is not None else "missing"
        await sealed.guard_logs(session, row.bot_id)
        bot = await session.get(Bot, row.bot_id)
        approval = row.approval_id
        if bot is None or not bot.enabled or sealed.is_locked(row):
            # 助理已停用或数据密钥已销毁：问题读不出来，无法派出，也没有会话可以投回。
            why = sealed.LOCKED_CODE if sealed.is_locked(row) else "assistant_inactive"
            row.status, row.error_code = "failed", why
            await session.commit()
            await execute._report(cfg, approval, "failed", why)
            return "failed"
        if cfg is None or not approval:
            status = await execute._close(session, row, bot, "failed", service.UNAVAILABLE)
            await execute._report(cfg, approval, status, service.UNAVAILABLE)
            return status
        calls = list((row.args_enc or {}).get("calls") or [])
        if row.execute_started_at is None:
            req_hash = tower.request_hash(
                cfg, row.external_id, tower.request_body(dispatch.TOOL, calls), bot.bot_key
            )
            try:
                redeemed = await tower.redeem(
                    cfg, await tower.token(cfg, approval), req_hash, bot.bot_key
                )
            except tower.TowerError as exc:
                if exc.unavailable:
                    raise execute.Retry from None
                # 令牌已用、已过期、请求对不上：这次批准作废，不派出。
                status = await execute._close(session, row, bot, "failed", exc.code)
                await execute._report(cfg, approval, status, exc.code)
                return status
            if redeemed != approval:
                status = await execute._close(session, row, bot, "failed", "approval_mismatch")
                await execute._report(cfg, approval, status, "approval_mismatch")
                return status
            row.execute_started_at = utcnow()
            await session.commit()

    code: str | None = None
    async with factory() as session:
        row = await session.get(
            AssistantApproval, approval_id, with_for_update=True, populate_existing=True
        )
        bot = await session.get(Bot, row.bot_id) if row is not None else None
        if row is None or bot is None:
            return "missing"
        if row.status != "approved":
            # 别处（例如超时清理）已经给了结论：不再记一次。
            return row.status
        if await session.get(AssistantJob, row.id) is None:
            call = calls[0] if calls else {}
            arguments = call.get("arguments") or {}
            try:
                target, _ = await dispatch.authorize(
                    session, bot, uuid.UUID(str(arguments.get("bot_id")))
                )
                await dispatch.send(
                    session,
                    bot,
                    target,
                    str(arguments.get("question") or ""),
                    allow_write=True,
                    attended=not call.get("unattended"),
                    approved=True,
                    job_id=row.id,
                )
            except (dispatch.DispatchError, jobs.JobError) as exc:
                code = exc.code
            except ValueError:
                code = "bot_not_found"
        if code is not None:
            status = await execute._close(session, row, bot, "failed", code)
        else:
            # 批准并派出之后不单独投一轮：答复由求助的结果回合带回助理会话。
            row.status, row.error_code, row.executed_at = "executed", None, utcnow()
            await session.commit()
            status = "executed"
    await execute._report(cfg, approval, status, code)
    return status

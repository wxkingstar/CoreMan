"""A member's own agent calls proxied business systems through the catalog MCP.

The agent (Claude Code, Codex …) holds the member's CoreMan OAuth token with the `mcp` scope. Reads
run at once as the member. A `write`, `destructive` or `financial` call is only staged, one per
card: the public bot (the
system setting `work_task_notifier_bot_id`) sends the member a confirmation card in its private
chat, the same card AI employees' writes get, and only a click there by the member runs exactly
what was staged. The agent learns the outcome from `systems_call_result`. Nothing the agent holds
can confirm: there is no API for it.

Calls are not tied to a task: the OAuth grant takes the task's place in `system_calls` and the
token issue records, and each member may make `systems.proxy_calls_per_turn` proxied calls to a
system per hour.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth.system_access import catalog_systems, token_subject
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, BusinessSystem, InteractionState, Setting, SystemCall, User
from coreman.core.db.models.oauth import OAuthClient, OAuthGrant
from coreman.core.systems_catalog import confirm, proxy, service
from coreman.core.systems_catalog.context import OWN_AGENT_TOKEN_TTL, Context
from coreman.core.timeutils import aware_utc, utcnow

KIND = confirm.MEMBER_KIND
CARD_EVENT = KIND
AAD = "member_write.v1"
# Writes a member may have waiting for confirmation at once.
MAX_PENDING = 20
# `systems_call_result` waits this long for the click before answering `pending`.
WAIT_SECONDS = 20.0
POLL_SECONDS = 1.0
# A confirmed write still without an outcome after this long was cut short (the worker died).
RUN_LOST = timedelta(minutes=2)
RESULT_MAX = 4000
BUDGET_WINDOW = timedelta(hours=1)

DESCRIPTION = (
    "Call an operation of a system marked as platform-proxied (call_mode proxy), as you, the "
    "member. Check it with systems_describe first. Reads run directly. Writes (write, "
    "destructive, financial) do not run now: the platform sends you a confirmation card in "
    "Feishu and runs the request only after you click confirm there; then call "
    "systems_call_result with the confirmation_id to get the outcome. File uploads and batch "
    "are not available here."
)
RESULT_DESCRIPTION = (
    "The outcome of a write staged by systems_call: waits up to 20 seconds for the member to "
    "confirm or cancel it on the Feishu card. status pending means not answered yet: tell the "
    "member to check the card, then call again. ok / failed / unknown come with the business "
    "system's response (unknown: it may have taken effect, read it back before anything else); "
    "refused, cancelled and expired mean nothing ran."
)
PENDING = (
    "没有执行。已通过飞书给你本人发了一张确认卡，在卡片上点「确认」后平台才会执行。告诉用户去飞书"
    "确认，然后用 systems_call_result 传 confirmation_id 等结果；不要说已经完成，也不要换别的方式"
    "做同一件事。"
)
UNAVAILABLE = (
    "没有执行。平台没法给你发飞书确认卡（管理员没有设置「公共机器人」、它不是启用的飞书 AI 员工，"
    "或你没有飞书身份）。写操作请到业务系统里自己操作，或在飞书里让 AI 员工去做。"
)
UNSUPPORTED = "成员本机调用不支持上传文件和批量提交。请到业务系统里操作，或在飞书里让 AI 员工去做。"
FOOTER = "改动说明由 AI 整理，以详情里的请求为准。只有你本人能确认，60 分钟内有效。"


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)
    confirmation_id: str = Field(min_length=1, max_length=64)


def _aad(state_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return f"{AAD}:{state_id}:{user_id}"


def seal(cipher: Cipher, state_id: uuid.UUID, user_id: uuid.UUID, items: list[Any]) -> str:
    return cipher.encrypt(json.dumps(items, ensure_ascii=False), _aad(state_id, user_id))


def unseal(cipher: Cipher, state: InteractionState) -> list[dict[str, Any]]:
    user_id = uuid.UUID(str(state.state["user_id"]))
    items = json.loads(cipher.decrypt(str(state.state["enc"]), _aad(state.id, user_id)))
    assert isinstance(items, list)
    return items


CARD_REF_PREFIX = "personal:member-write:"


def card_ref(state_id: uuid.UUID) -> str:
    return f"{CARD_REF_PREFIX}{state_id}"


def parse_card_ref(task_id: str) -> uuid.UUID | None:
    """The interaction state a card's `task_id` names, when it is a member-write card."""
    if not task_id.startswith(CARD_REF_PREFIX):
        return None
    raw = task_id.removeprefix(CARD_REF_PREFIX)
    try:
        state_id = uuid.UUID(raw)
    except ValueError:
        return None
    return state_id if str(state_id) == raw else None


async def build_context(
    session: AsyncSession,
    issuance: service.Issuance,
    user: User,
    grant: OAuthGrant,
) -> Context:
    """The proxy context of a member's own agent. Raises SubjectUnavailable when the member has no
    identity the business systems know."""
    subject = await token_subject(session, user)
    systems = await catalog_systems(session)
    client = await session.get(OAuthClient, grant.client_id)
    return Context(
        session=session,
        issuance=issuance,
        operator=service.Operator(user=user, subject=subject, oauth_grant_id=grant.id),
        systems=systems,
        # Writes are confirmed by the member on a card, so every proxied system takes them.
        writable=frozenset(s.key for s in systems if s.token_delivery == "proxy"),
        token_ttl=OWN_AGENT_TOKEN_TTL,
        # No object storage: large results are cut, since their download needs a task.
        settings=None,
        client_name=(client.client_name if client is not None else "")[:60],
    )


async def _over_budget(ctx: Context, system: BusinessSystem) -> dict[str, Any] | None:
    limit = system.proxy_calls_per_turn
    used = await ctx.session.scalar(
        select(func.count())
        .select_from(SystemCall)
        .where(
            SystemCall.user_id == ctx.member.user.id,
            SystemCall.system_key == system.key,
            SystemCall.task_id.is_(None),
            SystemCall.called_at > utcnow() - BUDGET_WINDOW,
        )
    )
    if int(used or 0) < limit:
        return None
    return {
        "error": "catalog_budget_exhausted",
        "stop": True,
        "system": system.key,
        "message": f"你一小时内对这个系统的代理调用已达 {limit} 次。停止调用这个系统，"
        "用已经拿到的信息完成；还没做完的稍后再继续。",
    }


async def call(ctx: Context, args: proxy.Call) -> dict[str, Any]:
    """`systems_call` from a member's own agent."""
    if args.batch is not None:
        return {"error": "unsupported_for_member", "message": UNSUPPORTED}
    found = await proxy.operation(ctx, args.system, args.operation_id)
    if isinstance(found, dict):
        return found
    system, loaded, op = found
    if proxy.takes_files(op):
        return {"error": "unsupported_for_member", "message": UNSUPPORTED}
    stop = await _over_budget(ctx, system)
    if stop is not None:
        return stop
    refused = await proxy.denial(ctx, system, args.operation_id, op)
    if refused is not None:
        return refused
    prepared = await proxy.build(ctx, system, loaded, op, args)
    if isinstance(prepared, dict):
        return prepared
    if op["risk"] in proxy.CONFIRMED_RISKS:
        return await propose(ctx, prepared, args)
    return await proxy.send(ctx, prepared)


async def public_bot(session: AsyncSession) -> Bot | None:
    """The bot that speaks for the platform when no particular AI employee should."""
    from coreman.core.assistants.records import is_assistant
    from coreman.core.work_tasks.cards import NOTIFIER_SETTING

    setting = await session.get(Setting, NOTIFIER_SETTING)
    try:
        bot_id = uuid.UUID(str(setting.value)) if setting is not None and setting.value else None
    except ValueError:
        return None
    bot = await session.get(Bot, bot_id) if bot_id else None
    if bot is None or not bot.enabled or bot.platform != "feishu" or is_assistant(bot):
        return None
    return bot


def _open(user_id: uuid.UUID) -> Any:
    return (
        select(func.count())
        .select_from(InteractionState)
        .where(
            InteractionState.kind == KIND,
            InteractionState.status == "open",
            InteractionState.expires_at > utcnow(),
            InteractionState.state["user_id"].astext == str(user_id),
        )
    )


async def propose(ctx: Context, p: proxy.Prepared, args: proxy.Call) -> dict[str, Any]:
    """Stage a checked write and send its card to the member."""
    from coreman.core.api_tasks.service import feishu_user_id

    session, member = ctx.session, ctx.member
    bot = await public_bot(session)
    pid = await feishu_user_id(session, member.user.id)
    if bot is None or not pid:
        await proxy.record(ctx, p.system, p.operation_id, p.op, "denied")
        return {"error": "confirmation_unavailable", "message": UNAVAILABLE}
    if int(await session.scalar(_open(member.user.id)) or 0) >= MAX_PENDING:
        await proxy.record(ctx, p.system, p.operation_id, p.op, "denied")
        return {
            "error": "too_many_pending",
            "message": f"你已有 {MAX_PENDING} 个写操作在等确认。先在飞书里处理掉，再提交新的。",
        }
    item = {
        "system": p.system.key,
        "system_name": p.system.name,
        "operation_id": p.operation_id,
        "summary": p.op.get("summary") or "",
        "method": p.op["method"],
        "path": p.op["path"],
        "risk": p.op["risk"],
        "op_digest": confirm.op_digest(p.op),
        "path_params": args.path_params,
        "query": args.query,
        "headers": p.headers or None,
        "body": args.body,
        "note": args.note.model_dump(exclude_none=True) if args.note else None,
        "files": [],
        # Fixed now, so the business system sees one key however often it is run.
        "key": str(uuid.uuid4()),
    }
    if not confirm.fits([item]):
        await proxy.record(ctx, p.system, p.operation_id, p.op, "denied")
        return {
            "error": "too_large_to_review",
            "message": "这个请求放不进一张确认卡。拆成更小的请求，或到业务系统里操作。",
        }
    state_id = uuid.uuid4()
    client = ctx.client_name or "本机客户端"
    state = InteractionState(
        id=state_id,
        bot_id=bot.id,
        kind=KIND,
        scope_key=str(state_id),
        status="open",
        state={
            "user_id": str(member.user.id),
            "grant_id": str(member.oauth_grant_id),
            "platform_user_id": pid,
            "card_ref": card_ref(state_id),
            "client": client,
            "count": 1,
            "enc": seal(ctx.issuance.cipher, state_id, member.user.id, [item]),
        },
    )
    session.add(state)
    body = confirm.card(
        0,
        state_id,
        [item],
        ref=card_ref(state_id),
        event=CARD_EVENT,
        preface=f"来自你本机的 {confirm._plain(client, 40)}（客户端自报名称）",
        footer=FOOTER,
    )
    await confirm.send_card(session, state, body, now=utcnow())
    await proxy.record(ctx, p.system, p.operation_id, p.op, "proposed")
    return {
        "status": "pending_confirmation",
        "system": p.system.key,
        "operation_id": p.operation_id,
        "confirmation_id": str(state_id),
        "expires_in": int(confirm.TTL.total_seconds()),
        "message": PENDING,
    }


def _outcome(state: InteractionState, now: datetime) -> dict[str, Any] | None:
    """What became of a staged write; None while it waits for the member."""
    staged = state.state
    if isinstance(staged.get("result"), dict):
        return dict(staged["result"])
    if state.status == "cancelled":
        if staged.get("undelivered"):
            return {"status": "cancelled", "message": "确认卡没能发到飞书，这个写操作没有执行。"}
        return {"status": "cancelled", "message": "用户在卡片上取消了，没有执行。不要重试。"}
    if state.status == "expired" or (
        state.status == "open"
        and state.expires_at is not None
        and aware_utc(state.expires_at) <= now
    ):
        return {"status": "expired", "message": "确认卡已过期，没有执行。需要的话重新提交。"}
    if state.status == "submitted":
        started = staged.get("sending_at")
        if isinstance(started, str) and now - aware_utc(datetime.fromisoformat(started)) > RUN_LOST:
            return {
                "status": "unknown",
                "message": "执行中途被打断，结果未知，可能已经生效。先用读操作核对，不要直接重做。",
            }
    return None


async def result(ctx: Context, args: Result) -> dict[str, Any]:
    """`systems_call_result`: waits briefly for the click, then reports."""
    try:
        state_id = uuid.UUID(args.confirmation_id)
    except ValueError:
        state_id = None
    # Read before the loop: the rollback below expires the member's ORM rows, and reading an
    # expired attribute would lazy-load outside the greenlet (MissingGreenlet).
    user_id = str(ctx.member.user.id)
    grant_id = str(ctx.member.oauth_grant_id)
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        state = (
            await ctx.session.get(InteractionState, state_id, populate_existing=True)
            if state_id
            else None
        )
        if (
            state is None
            or state.kind != KIND
            or state.state.get("user_id") != user_id
            or state.state.get("grant_id") != grant_id
        ):
            return {
                "error": "confirmation_not_found",
                "message": "没有这个待确认的写操作（编号不对，或已经清理）。",
            }
        outcome = _outcome(state, utcnow())
        running = state.status == "submitted"
        # Release the snapshot: the click is committed by another process.
        await ctx.session.rollback()
        if outcome is not None:
            return {"confirmation_id": str(state_id), **outcome}
        if time.monotonic() >= deadline:
            return {
                "confirmation_id": str(state_id),
                "status": "pending",
                "message": "已确认，正在执行，稍后再查。"
                if running
                else "用户还没在飞书卡片上确认。提醒用户去确认，然后再调用一次。",
            }
        await asyncio.sleep(POLL_SECONDS)


def brief(value: dict[str, Any]) -> str:
    data = {k: v for k, v in value.items() if k not in ("system", "operation_id", "content_trust")}
    text = json.dumps(data, ensure_ascii=False, default=str)
    return text[:RESULT_MAX] + ("…" if len(text) > RESULT_MAX else "")


async def active_grant(session: AsyncSession, state: InteractionState) -> OAuthGrant | None:
    """The OAuth grant the write was staged under, while it is still in force."""
    try:
        grant = await session.get(OAuthGrant, uuid.UUID(str(state.state.get("grant_id"))))
    except ValueError:
        return None
    if (
        grant is None
        or grant.revoked_at is not None
        or str(grant.user_id) != state.state.get("user_id")
        or aware_utc(grant.refresh_expires_at) <= utcnow()
    ):
        return None
    return grant

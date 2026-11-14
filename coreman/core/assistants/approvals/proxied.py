"""个人助理无人在场时经平台代理的写操作：暂存成审批单，所有者批准后由平台原样执行。

两类：业务系统目录代理（`systems_call`）与云签名代理（阿里云）。所有者在场时这两个代理照旧直接
执行（低风险）或走各自的流程；无人在场的轮次里输入可能来自别人，写调用不再直接拒绝，而是：

1. 代理先做完它平时的检查（目录可见、参数合规、授权允许写；云接口判为写且风险评估为低风险），
   再调这里的 `submit_system` / `submit_cloud`：完整请求进加密的 `args_enc`，向 Tower 提交一张
   以所有者为发起人的单，不向上游发任何请求；
2. 批准后 worker 的执行任务调 `run`：重新核对助理、所有者与授权，核销令牌，再交回代理走与在场
   调用相同的服务端路径（同样的审计、令牌签发记录、风险与策略检查），执行的是暂存的那一份；
3. 结局经 `service.deliver` 作为一轮投回助理会话。

复用审批闸门的表、回调、对账、过期、撤回与提醒（`service`）；目标系统与写类个人工具相同
（`tower.TARGET`），线上不用另行登记。能提审批的只有本来就能提交飞书写审批的那几类轮次
（`may_stage`）；子任务、冷启动、调度公共员工、告警、所有者在场的轮次都不经过这里。
"""

from __future__ import annotations

import json
import unicodedata
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.assistants import kinds, quoting, sealed
from coreman.core.assistants.approvals import catalog, content, execute, service, tower
from coreman.core.assistants.records import inactive_assistant, is_assistant
from coreman.core.config import Settings
from coreman.core.db.models import (
    AssistantApproval,
    Bot,
    BotSystemGrant,
    BusinessSystem,
    Task,
    User,
)
from coreman.core.logging import get_logger
from coreman.core.prompting.sanitize import sanitize_user_input
from coreman.core.timeutils import utcnow

if TYPE_CHECKING:
    from coreman.core.systems_catalog.context import Context as CatalogContext
    from coreman.core.systems_catalog.service import Issuance

log = get_logger(__name__)

TOOLS = frozenset(catalog.PROXIED)
# `assistant_approvals.channel` 的现有取值；这类单靠工具名区分。
CHANNEL = "other"
BAR = content.BAR
# 这两类单同时待批的上限：无人在场的代理写刷不满各类共用的 `service.MAX_PENDING`，
# 所有者在场时自己的审批总有位置。
MAX_PENDING = 5
TOO_LARGE = "too_large_to_review"
# 发起背景里列出的交回事项：最多几件、每件摘录多少字。
ORIGIN_ITEMS = 5
EXCERPT_LIMIT = 200
NAME_LIMIT = 80
# 结果回合里带回的响应：太大的只留开头。
RESULT_LIMIT = 8000
HINT = (
    "已提交给所有者审批，现在没有执行。批准后平台会按这次的请求原样执行，并在本会话里告诉你"
    "结果；不要重试，也不要换别的方式（令牌直连、MCP、别的接口）做同一件事，继续处理别的事。"
)
NOTICE = (
    "说明：这一轮是你的个人助理在你不在场时跑的，不是你本人让它做的。批准后平台以你的身份、"
    "按上面暂存的完整请求原样执行一次，结果交回助理；执行前会重新核对授权，授权已撤销、写入"
    "已关闭、系统已停用或助理已停用时不执行。"
)
ORIGINS = {
    kinds.HANDOVER_KIND: "平台交回盯消息时攒下的事项",
    kinds.JOB_RESULT_KIND: "子任务或其他 AI 员工的结论投回",
    kinds.APPROVAL_RESULT_KIND: "另一张审批单的结局投回",
    kinds.COMPILE_KIND: "下班整理",
}
_ITEM_TITLES = {"p2p": "私聊消息", "group": "群聊消息"}
# 展示时要转义的 Unicode 类别：控制、格式、行分隔、段分隔。
_HIDDEN = frozenset({"Cc", "Cf", "Zl", "Zp"})


def may_stage(task: Task, bot: Bot, user: User) -> bool:
    """这一轮经代理的写操作能不能提交给所有者审批。

    只有个人助理无人在场、而且本来就能提交飞书写审批（有 `approval` 组）的轮次能提：交回、
    结论投回、审批结果回合。子任务与冷启动没有 `approval` 组，照旧拒绝。身份必须是所有者本人，
    助理必须是启用中的。
    """
    from coreman.core.assistants import tools as assistant_tools  # 总注册表依赖面广，延迟导入

    return (
        kinds.unattended(task.kind)
        and "approval" in assistant_tools.groups_for(task.kind)
        and task.bot_id == bot.id
        and bot.enabled
        and is_assistant(bot)
        and not inactive_assistant(bot)
        and bot.owner_id is not None
        and bot.owner_id == user.id
    )


def _one_line(text: Any, limit: int) -> str:
    """别人写的一小段（会话名、摘录、目录里的名字）：丢掉冒充平台标签的行，压成一行，换掉方括号。"""
    flat = " ".join(sanitize_user_input(str(text or "")).split()).translate(quoting.BRACKETS)
    flat = _visible(flat)
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _visible(text: str) -> str:
    """展示用：控制字符、格式字符（方向覆盖、零宽字符等）与行、段分隔符转义成可见的 `\\uXXXX`。

    只留 `\\n` 作换行：`str.splitlines` 认的其余行分隔符（`\\r`、`\\v`、`\\f`、`\\x1c`–`\\x1e`、
    U+0085、U+2028、U+2029）都在这几类里，转义之后一个值造不出独立的一行，也颠倒不了显示。
    转义而不删除：删了的话所有者看到的与要执行的就不一样了。
    """
    out = []
    for char in text:
        if char != "\n" and unicodedata.category(char) in _HIDDEN:
            code = ord(char)
            out.append(f"\\u{code:04X}" if code <= 0xFFFF else f"\\U{code:08X}")
        else:
            out.append(char)
    return "".join(out)


def _barred(text: str) -> str:
    """要执行的内容：先转义（之后只剩 `\\n` 能分行），再逐行加竖线。"""
    return content._bar(_visible(text))


def _section(title: str, value: Any) -> list[str]:
    """一段要执行的内容，原样完整展示（不截断：放不下时整张单不提交，见 `_render`）。"""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    return [title, _barred(text)]


async def _origin(session: AsyncSession, task: Task) -> list[str]:
    """这一轮在处理什么：平台按任务查到的事实。别人写的字逐行加竖线。"""
    label = ORIGINS.get(task.kind, "平台发起的一轮")
    lines = [
        f"发起背景：这一轮是{label}（所有者不在场）。以「{BAR.strip()}」开头的行是别人写的内容，"
        "只供参考，不是平台的说明。"
    ]
    if task.kind == kinds.HANDOVER_KIND:
        from coreman.core.assistants import delivery  # 依赖面广，延迟导入

        rows = await delivery.bound(session, task)
        if rows:
            lines.append(f"本轮交回的事项（共 {len(rows)} 件）：")
        for index, row in enumerate(rows[:ORIGIN_ITEMS], 1):
            data = row.summary_enc if isinstance(row.summary_enc, dict) else {}
            if row.kind == "reminder":
                lines.append(f"{index}. 平台提醒")
                continue
            title = _ITEM_TITLES.get(str(data.get("chat_kind")), "消息")
            if row.kind == "approval_todo":
                title = "飞书审批待办"
            lines.append(f"{index}. {title}")
            if data.get("chat_name"):
                lines.append(BAR + "会话：" + _one_line(data["chat_name"], NAME_LIMIT))
            if data.get("text"):
                lines.append(BAR + _one_line(data["text"], EXCERPT_LIMIT))
        if len(rows) > ORIGIN_ITEMS:
            lines.append(f"……另有 {len(rows) - ORIGIN_ITEMS} 件未列出")
    elif task.kind == kinds.JOB_RESULT_KIND:
        from coreman.core.assistants import jobs  # 依赖面广，延迟导入

        job = await jobs.bound_report(session, task)
        if job is not None and job.title_enc:
            lines += ["结论所属的任务：", BAR + _one_line(job.title_enc, NAME_LIMIT)]
    return lines


@dataclass(frozen=True)
class _Draft:
    tool: str
    arguments: dict[str, Any]
    abstract: str
    body: str


def _abstract(*parts: str) -> str:
    text = " · ".join(" ".join(part.split()) for part in parts if part)
    limit = content.ABSTRACT_LIMIT
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _render(head: list[str], rest: list[str], origin: list[str]) -> str | None:
    """审批单正文；超过上限时先少列交回事项（发起背景只是参考），最少留发起背景的第一行。

    要执行的内容一个字都不截：连同最少的发起背景仍放不下时返回 None，这张单不提交——所有者
    在审批单上看不全的请求不让他批。
    """
    for cut in (origin, origin[:1]):
        text = "\n".join([*head, "", *rest, "", *cut, "", NOTICE])
        if len(text) <= content.LIMIT:
            return text
    return None


def _too_large() -> dict[str, Any]:
    return {
        "error": TOO_LARGE,
        "limit": content.LIMIT,
        "hint": "这次请求的内容太长，所有者在审批单上看不全，这一轮不提交，也没有执行。把要做的事"
        "写进给所有者的结论里，或拆成能在审批单上完整展示的小请求再调用。",
    }


async def _submit(
    session: AsyncSession,
    *,
    task: Task,
    bot: Bot,
    owner: User,
    draft: _Draft,
    cfg: tower.Config | None,
) -> dict[str, Any]:
    """暂存并向 Tower 提交；不执行。调用方提交事务（被拒时嵌套事务已回滚，不留行）。"""
    if cfg is None:
        return {"error": service.UNAVAILABLE, "hint": "这个部署没有配置审批系统，写操作不可用。"}
    if not owner.login_name:
        return {
            "error": "no_login_name",
            "hint": "所有者在平台里没有登录名，无法提交审批：请管理员在成员资料里补上。",
        }
    calls = [
        {
            "arguments": draft.arguments,
            "chat_id": None,
            "anchor_message_id": None,
            "thread_id": None,
            # 执行前核对：所有者换了人就不执行。
            "owner_id": str(owner.id),
            "unattended": True,
        }
    ]
    try:
        digest = await sealed.content_mac(
            session, bot.id, service.HASH_PURPOSE, service._dedupe_text(draft.tool, calls)
        )
    except sealed.ContentLocked:
        return {"error": sealed.LOCKED_CODE}
    existing = await session.scalar(
        select(AssistantApproval.id)
        .where(
            AssistantApproval.bot_id == bot.id,
            AssistantApproval.args_hash == digest,
            AssistantApproval.status.in_(service.LIVE),
        )
        .limit(1)
    )
    if existing is not None:
        # 同一份请求已有在途的单（同一轮里重复调用，或之前的轮次提交过）：不重复建单。
        return {
            "status": "pending_approval",
            "approval_id": str(existing),
            "duplicate": True,
            "hint": HINT,
        }
    waiting = await session.scalar(
        select(func.count())
        .select_from(AssistantApproval)
        .where(AssistantApproval.bot_id == bot.id, AssistantApproval.status == "pending")
    )
    if (waiting or 0) >= service.MAX_PENDING:
        return {
            "error": "too_many_pending",
            "limit": service.MAX_PENDING,
            "hint": "同时待批的审批单已到上限：这次没有提交，也没有执行。把要做的事写进给所有者的"
            "结论里，或用 approval_withdraw 撤回不再需要的。",
        }
    proxied = await session.scalar(
        select(func.count())
        .select_from(AssistantApproval)
        .where(
            AssistantApproval.bot_id == bot.id,
            AssistantApproval.status == "pending",
            AssistantApproval.tool.in_(TOOLS),
        )
    )
    if (proxied or 0) >= MAX_PENDING:
        # 只数这两类：各类共用的 10 张里给所有者在场时自己的审批留出位置。
        return {
            "error": "too_many_pending",
            "limit": MAX_PENDING,
            "hint": "所有者不在场时提交的业务系统与云服务写操作，同时待批已到上限：这次没有提交，"
            "也没有执行。把要做的事写进给所有者的结论里，或用 approval_withdraw 撤回不再需要的。",
        }
    row_id = uuid.uuid4()
    external_id = service.EXTERNAL_PREFIX + row_id.hex
    body = tower.request_body(draft.tool, calls)
    try:
        # 审批系统没收下就连同暂存的行一起回滚：不留一张谁也批不了的单。
        async with session.begin_nested():
            row = AssistantApproval(
                id=row_id,
                bot_id=bot.id,
                tool=draft.tool,
                external_id=external_id,
                origin_event_id=task.inbound_event_id,
                session_key=task.session_key or "",
                args_enc={
                    "calls": calls,
                    "abstract": draft.abstract,
                    "background": "",
                    "research": "",
                },
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
                requester=str(owner.login_name),
                caller=bot.bot_key,
                abstract=draft.abstract,
                content=draft.body,
                body=body,
                expires_at=row.expires_at,
            )
            row.approval_id = sent.approval_id
    except tower.TowerError as exc:
        log.warning("assistant_proxied_submit_failed", approval=str(row_id), code=exc.code)
        return service._tower_failure(exc)
    log.info("assistant_proxied_submitted", approval=str(row_id), tool=draft.tool)
    return {"status": "pending_approval", "approval_id": str(row_id), "hint": HINT}


async def submit_system(
    ctx: CatalogContext,
    system: BusinessSystem,
    op: dict[str, Any],
    *,
    operation_id: str,
    path_params: dict[str, Any] | None,
    query: dict[str, Any] | None,
    body: Any,
    url: str,
) -> dict[str, Any]:
    """目录代理在无人在场时的写调用：参数已按目录校验、授权本来允许写。只暂存，不调业务系统。"""
    session = ctx.session
    task = await session.get(Task, ctx.member.task_id)
    bot = await session.get(Bot, ctx.member.bot_id)
    owner = ctx.member.user
    if task is None or bot is None or not may_stage(task, bot, owner):
        return {"error": "owner_turn_required"}
    method = str(op["method"])
    summary = _one_line(op.get("summary") or "", NAME_LIMIT)
    name = _one_line(system.name, NAME_LIMIT)
    arguments = {
        "system": system.key,
        "operation_id": operation_id,
        "path_params": path_params,
        "query": query,
        "body": body,
        "method": method,
        "url": url,
    }
    head = [
        "渠道：业务系统（平台代理）",
        f"对象：{name}（{system.key}）",
        f"动作：{summary or operation_id}（operation_id={operation_id}，风险等级 write）",
        "请求：",
        _barred(f"{method} {url}"),
    ]
    rest = ["将要执行的内容（完整）："]
    if path_params:
        rest += _section("路径参数：", path_params)
    if query:
        rest += _section("查询参数：", query)
    if body is not None:
        rest += _section("请求体：", body)
    if len(rest) == 1:
        rest.append("（没有参数）")
    text = _render(head, rest, await _origin(session, task))
    if text is None:
        return _too_large()
    draft = _Draft(
        catalog.SYSTEM_CALL,
        arguments,
        _abstract(
            catalog.PROXIED[catalog.SYSTEM_CALL], name or system.key, summary or operation_id
        ),
        text,
    )
    return await _submit(
        session, task=task, bot=bot, owner=owner, draft=draft, cfg=ctx.approval_cfg
    )


async def submit_cloud(
    session: AsyncSession,
    *,
    task: Task,
    bot: Bot,
    owner: User,
    product: str,
    params: dict[str, str],
    reasons: tuple[str, ...],
    cfg: tower.Config | None,
) -> dict[str, Any]:
    """云签名代理在无人在场时的低风险写：已判为写、风险评估为低风险。只暂存，不调阿里云。"""
    if not may_stage(task, bot, owner):
        return {"error": "owner_turn_required"}
    action = str(params.get("Action") or "")
    # 每个值都按 JSON 字符串展示（换行、引号转义），再把 JSON 不转义的格式字符与行分隔符
    # （U+2028、U+202E 等）转义成可见形式：一个参数恰好一行。参数名只有 ASCII 字母数字与 `._`
    # （`cloud_proxy.service.parse_body` 已校验）。
    lines = [
        f"{key}={json.dumps(str(params[key]), ensure_ascii=False)}"
        for key in sorted(params)
        if key != "Action"
    ]
    head = [
        "渠道：云服务（阿里云，平台签名代理）",
        f"对象：{product}",
        f"动作：{action}",
        "风险评估：低风险写操作（所有者在场时会直接执行的那一类）",
    ]
    rest = ["将要执行的参数（完整）："]
    rest.append(_barred("\n".join(lines)) if lines else "（除 Action 外没有参数）")
    if reasons:
        rest += ["风险评估说明：", _barred("\n".join(reasons))]
    text = _render(head, rest, await _origin(session, task))
    if text is None:
        return _too_large()
    draft = _Draft(
        catalog.CLOUD_CALL,
        {"product": product, "params": dict(params)},
        _abstract(catalog.PROXIED[catalog.CLOUD_CALL], product, action),
        text,
    )
    return await _submit(session, task=task, bot=bot, owner=owner, draft=draft, cfg=cfg)


def _trimmed(result: dict[str, Any]) -> dict[str, Any]:
    """结果回合带回的响应：超过上限只留开头（结果存档另有总上限）。"""
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) <= RESULT_LIMIT:
        return result
    return {"truncated": True, "head": text[:RESULT_LIMIT]}


async def _precheck(
    session: AsyncSession, row: AssistantApproval, bot: Bot, settings: Settings
) -> str | None:
    """批准之后、核销之前的复查（只读，不碰上游）：不能执行时返回原因代码。"""
    if inactive_assistant(bot) or not is_assistant(bot):
        return "assistant_inactive"
    calls = list((row.args_enc or {}).get("calls") or [])
    if len(calls) != 1:
        return "invalid_tool_or_arguments"
    call = calls[0]
    owner = await session.get(User, bot.owner_id) if bot.owner_id is not None else None
    if (
        owner is None
        or str(owner.id) != str(call.get("owner_id"))
        or owner.status != "active"
        or owner.source == "bootstrap"
    ):
        return "owner_changed"
    if row.tool == catalog.SYSTEM_CALL:
        from coreman.core.auth.system_access import granted_systems

        key = str(call["arguments"].get("system") or "")
        grant = await session.get(BotSystemGrant, (bot.id, key), populate_existing=True)
        if grant is None:
            return "grant_revoked"
        if not grant.allow_write:
            return "write_not_allowed"
        systems = await granted_systems(session, bot.id)
        found = next((s for s in systems if s.key == key), None)
        if found is None or found.token_delivery != "proxy":
            return "system_unavailable"
    elif not settings.cloud_proxy_ready:
        return "cloud_proxy_disabled"
    return None


async def _execute_system(
    factory: async_sessionmaker[AsyncSession],
    bot_id: uuid.UUID,
    task: Task,
    arguments: dict[str, Any],
    issuance: Issuance,
) -> dict[str, Any]:
    """以所有者身份走目录代理的同一条路径（授权、目录、策略、参数校验、令牌、审计）。"""
    from coreman.core.auth.system_access import (
        SubjectUnavailable,
        granted_systems,
        token_subject,
    )
    from coreman.core.systems_catalog import context as catalog_context
    from coreman.core.systems_catalog import policy, proxy
    from coreman.core.systems_catalog import service as catalog_service

    async with factory() as session:
        bot = await session.get(Bot, bot_id)
        owner = await session.get(User, bot.owner_id) if bot and bot.owner_id else None
        if bot is None or owner is None:
            return {"error": "owner_changed"}
        try:
            subject = await token_subject(session, owner)
        except SubjectUnavailable as exc:
            return {"error": "subject_unavailable", "reason": exc.reason}
        granted = frozenset(
            await session.scalars(
                select(BotSystemGrant.system_key).where(
                    BotSystemGrant.bot_id == bot.id, BotSystemGrant.allow_write
                )
            )
        )
        ctx = catalog_context.Context(
            session=session,
            issuance=issuance,
            operator=catalog_service.Operator(
                user=owner, subject=subject, task_id=task.id, bot_id=bot.id
            ),
            systems=await granted_systems(session, bot.id),
            # 执行任务不是无人在场的类型：照员工当前的授权判断能不能写。
            writable=policy.writable_for(task, granted),
            token_ttl=bot.sse_timeout_seconds,
        )
        try:
            call = proxy.Call(
                system=arguments["system"],
                operation_id=arguments["operation_id"],
                path_params=arguments.get("path_params"),
                query=arguments.get("query"),
                body=arguments.get("body"),
            )
        except (KeyError, ValueError):
            return {"error": "invalid_tool_or_arguments"}
        approved = {
            "method": arguments.get("method"),
            "url": arguments.get("url"),
            "body": arguments.get("body"),
        }
        result = await proxy.call(ctx, call, approved=approved)
        await session.commit()
        return result


async def _execute_cloud(
    factory: async_sessionmaker[AsyncSession],
    bot_id: uuid.UUID,
    task: Task,
    arguments: dict[str, Any],
    settings: Settings,
    approval_id: str,
) -> dict[str, Any]:
    """走云签名代理的同一条路径：重新判定类别与风险、审计、签名转发。"""
    from coreman.core.cloud_proxy import service as cloud

    async with factory() as session:
        bot = await session.get(Bot, bot_id)
        owner = await session.get(User, bot.owner_id) if bot and bot.owner_id else None
        if bot is None or owner is None:
            return {"error": "owner_changed"}
        params = arguments.get("params")
        if not isinstance(params, dict):
            return {"error": "invalid_tool_or_arguments"}
        return await cloud.execute_approved(
            session,
            settings=settings,
            task=task,
            bot=bot,
            user=owner,
            product_key=str(arguments.get("product") or ""),
            params={str(k): str(v) for k, v in params.items()},
            approval_id=approval_id,
        )


async def run(
    factory: async_sessionmaker[AsyncSession],
    approval_id: uuid.UUID,
    task: Task,
    cfg: tower.Config | None,
    *,
    settings: Settings,
    issuance: Issuance,
) -> str:
    """执行一张已批准的代理写操作单，返回它最后的状态。只执行一次：三段事务与 `execute.run` 相同。

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
        bot = await session.get(Bot, row.bot_id, populate_existing=True)
        approval = row.approval_id
        if bot is None or not bot.enabled or sealed.is_locked(row):
            code = sealed.LOCKED_CODE if sealed.is_locked(row) else "assistant_inactive"
            row.status, row.error_code = "failed", code
            await session.commit()
            await execute._report(cfg, approval, "failed", code)
            return "failed"
        if row.execute_started_at is not None:
            # 上一次已经开始执行、没等到结果：不知道是否生效，绝不再执行一次。
            status = await execute._close(session, row, bot, "failed", "execution_interrupted")
            await execute._report(cfg, approval, status, "execution_interrupted")
            return status
        if cfg is None or not approval:
            status = await execute._close(session, row, bot, "failed", service.UNAVAILABLE)
            await execute._report(cfg, approval, status, service.UNAVAILABLE)
            return status
        refused = await _precheck(session, row, bot, settings)
        if refused is not None:
            status = await execute._close(session, row, bot, "failed", refused)
            await execute._report(cfg, approval, status, refused)
            return status
        calls = list((row.args_enc or {}).get("calls") or [])
        tool = row.tool
        req_hash = tower.request_hash(
            cfg, row.external_id, tower.request_body(tool, calls), bot.bot_key
        )
        try:
            redeemed = await tower.redeem(
                cfg, await tower.token(cfg, approval), req_hash, bot.bot_key
            )
        except tower.TowerError as exc:
            if exc.unavailable:
                raise execute.Retry from None
            status = await execute._close(session, row, bot, "failed", exc.code)
            await execute._report(cfg, approval, status, exc.code)
            return status
        if redeemed != approval:
            status = await execute._close(session, row, bot, "failed", "approval_mismatch")
            await execute._report(cfg, approval, status, "approval_mismatch")
            return status
        row.execute_started_at = utcnow()
        bot_id = bot.id
        await session.commit()

    arguments = dict(calls[0]["arguments"])
    try:
        if tool == catalog.SYSTEM_CALL:
            result = await _execute_system(factory, bot_id, task, arguments, issuance)
        else:
            result = await _execute_cloud(factory, bot_id, task, arguments, settings, approval)
    except Exception as exc:  # noqa: BLE001 结果必须落到审批单上，不能悬着
        log.warning("assistant_proxied_execute_error", type=type(exc).__name__)
        result = {"error": "execution_error", "type": type(exc).__name__}

    async with factory() as session:
        row = await session.get(
            AssistantApproval, approval_id, with_for_update=True, populate_existing=True
        )
        bot = await session.get(Bot, bot_id)
        if row is None or bot is None:
            return "missing"
        if row.status != "approved":
            # 别处（例如超时清理）已经给了结论：不再记一次。
            return row.status
        failure: str | None = None
        if "error" in result:
            failure = str(result.get("error") or "failed")
        elif result.get("ok") is False:
            failure = "upstream_http_error"
        if failure is not None:
            status = await execute._close(session, row, bot, "failed", failure, _trimmed(result))
        else:
            status = await execute._close(session, row, bot, "executed", None, _trimmed(result))
    await execute._report(cfg, approval, status, failure)
    return status

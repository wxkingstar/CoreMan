"""批准之后执行暂存的写操作：复查会话、核销令牌、按暂存参数原样执行、上报并投回结果。

由 worker 的 `assistant_approval_execute` 任务调用，不经模型。分三段事务：
1. 复查、取回附件并校验、核销，记下「开始执行」并提交——之后进程即使被杀，重跑也不会再执行一次；
2. 调飞书（不持有审批单的行锁）；
3. 记结果、排结果回合。

带文件的单：附件在提交时已复制到临时桶。第 1 段取回并核对大小与 SHA-256（对不上或取不到都
不执行），第 2 段把它重新落进文件中转区、签发新的中转编号，再交给原来的工具函数。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.assistants import sealed, sent, write_fuse
from coreman.core.assistants.approvals import attachments, catalog, checks, service, tower
from coreman.core.bus import tasks
from coreman.core.crypto import Cipher
from coreman.core.db.models import AssistantApproval, Bot, FeishuPersonalGrant, Task, UserReached
from coreman.core.feishu_personal.files import FileRelay
from coreman.core.feishu_personal.policy import Scope
from coreman.core.feishu_personal.service import PersonalError
from coreman.core.feishu_personal.toolbase import Call
from coreman.core.feishu_personal.tools import TOOLS as PERSONAL_TOOLS
from coreman.core.logging import get_logger
from coreman.core.timeutils import utcnow

log = get_logger(__name__)

# 批准（或开始执行）之后超过这么久、又没有在途的执行任务，就当作执行被中断。
STUCK_AFTER = timedelta(minutes=15)


class Retry(Exception):
    """审批系统或临时桶暂时连不上：这次什么都没做，稍后重跑。`code` 是放弃时记下的原因。"""

    def __init__(self, code: str = "tower_unavailable") -> None:
        super().__init__(code)
        self.code = code


async def _once(content: bytes) -> AsyncIterator[bytes]:
    yield content


async def _materialize(
    session: AsyncSession,
    files: FileRelay | None,
    scope: Scope,
    arguments: dict[str, Any],
    blobs: dict[str, bytes],
) -> dict[str, Any]:
    """把参数里的文件引用换回原工具认得的中转编号：文件重新落进中转区（一小时后过期）。"""
    names = attachments.ARGUMENTS & set(arguments)
    if not names or files is None:
        return arguments
    grant = await session.get(FeishuPersonalGrant, (scope.bot.id, scope.user_id))
    if grant is None:
        raise PersonalError("authorization_required")

    async def fresh(ref: dict[str, Any]) -> str:
        row = await files.save(
            session, _once(blobs[ref["key"]]), filename=ref["filename"], mime=ref["mime"]
        )
        return files.upload_id(grant, row)

    out = dict(arguments)
    for name in sorted(names):
        value = arguments[name]
        if isinstance(value, list):
            out[name] = [await fresh(ref) for ref in value]
        else:
            out[name] = await fresh(value)
    return out


async def owner_scope(session: AsyncSession, bot: Bot, task: Task) -> Scope | None:
    """以所有者身份调飞书用的核验结果：取自他对这个助理的授权记录，不来自任何调用方。"""
    if bot.owner_id is None:
        return None
    grant = await session.get(FeishuPersonalGrant, (bot.id, bot.owner_id), populate_existing=True)
    if grant is None or grant.status != "connected" or not grant.token_enc:
        return None
    reached = await session.get(UserReached, (bot.id, bot.owner_id))
    return Scope(
        task,
        bot,
        bot.owner_id,
        grant.tenant_key,
        grant.app_id,
        grant.open_id,
        grant.platform_user_id,
        reached.platform_chat_id if reached is not None else "",
    )


async def _remember_sent(
    factory: async_sessionmaker[AsyncSession], bot_id: uuid.UUID, result: dict[str, Any]
) -> None:
    """记下刚以所有者身份发出的消息的编号：盯消息读到它时不当作所有者亲自发言（见 `sent.py`）。

    另开一个短事务、立刻提交：盯消息随时可能读到这条消息，等不到执行的事务结束（合并发送时
    后面还有别的会话要发）。记不上只记日志——消息已经发出去了，不能因此把这张单记成失败。
    """
    message_id = result.get("message_id") if isinstance(result, dict) else None
    if not isinstance(message_id, str) or not message_id:
        return
    try:
        async with factory() as side:
            await sent.record(side, bot_id, [message_id])
            await side.commit()
    except Exception as exc:  # noqa: BLE001 只记类型
        log.warning("assistant_sent_record_failed", type=type(exc).__name__)


class Unrunnable(Exception):
    """一项的附件取不回来或对不上：这一项不能执行。`code` 是原因代码。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def calls_of(row: AssistantApproval) -> list[dict[str, Any]]:
    return list((row.args_enc or {}).get("calls") or [])


async def check_calls(
    session: AsyncSession, cipher: Cipher, scope: Scope, tool: str, calls: list[dict[str, Any]]
) -> None:
    """执行前复查一项：重新读会话、核对锚点与目标（与提交时同一套检查）。

    Raises:
        checks.Rejected: 会话或目标变了，这一项不该再执行
        PersonalError: 所有者的授权失效
    """
    reader = Call(session, cipher, scope)
    assistant_id = scope.bot.id

    async def ours(message_ids: list[str]) -> frozenset[str]:
        # 同会话的另一张单刚由平台代发出去：那条消息不算所有者亲自回复，这张单不因此过时。
        return await sent.among(session, assistant_id, message_ids)

    for one in calls:
        await checks.check_call(reader, tool, one, owner_open_id=scope.open_id, ours=ours)


async def fetch(
    files: FileRelay | None, calls: list[dict[str, Any]], blobs: dict[str, bytes]
) -> None:
    """取回一项的附件并核对大小与 SHA-256，放进 `blobs`。

    Raises:
        Retry: 临时桶暂时连不上（什么都没变，稍后重来）
        Unrunnable: 部署没配临时桶、附件已被清理或内容不是提交时的那一份
    """
    refs = attachments.references(calls)
    if not refs:
        return
    store = attachments.store(files.settings) if files is not None else None
    if store is None:
        raise Unrunnable("attachments_not_supported")
    try:
        for ref in refs:
            blobs[ref["key"]] = await attachments.get(store, ref)
    except attachments.Unavailable:
        raise Retry("attachment_store_unavailable") from None
    except (attachments.Missing, attachments.Changed) as exc:
        missing = isinstance(exc, attachments.Missing)
        raise Unrunnable("attachment_missing" if missing else "attachment_changed") from None


async def perform(
    factory: async_sessionmaker[AsyncSession],
    cipher: Cipher,
    bot_id: uuid.UUID,
    task: Task,
    tool: str,
    calls: list[dict[str, Any]],
    blobs: dict[str, bytes],
    files: FileRelay | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """以所有者身份按暂存的参数原样执行一项，自己一个短事务。返回 (各次结果, 失败)。

    调用方已复查、已记下「开始执行」并提交：这里不再看审批单，也不持有它的行锁。
    """
    results: list[dict[str, Any]] = []
    failure: dict[str, Any] | None = None
    async with factory() as session:
        bot = await session.get(Bot, bot_id)
        scope = await owner_scope(session, bot, task) if bot is not None else None
        item = PERSONAL_TOOLS.get(tool)
        if scope is None or item is None:
            failure = {"error": "authorization_required"}
        else:
            with write_fuse.allow(bot_id):
                for one in calls:
                    try:
                        arguments = await _materialize(
                            session, files, scope, one["arguments"], blobs
                        )
                        args = item.args.model_validate(arguments)
                        results.append(await item.run(args, Call(session, cipher, scope, files)))
                        if tool in catalog.SENDS:
                            await _remember_sent(factory, bot_id, results[-1])
                    except PersonalError as exc:
                        failure = exc.payload()
                        break
                    except ValidationError:
                        failure = {"error": "invalid_tool_or_arguments"}
                        break
                    except Exception as exc:  # noqa: BLE001 结果必须落到审批单上，不能悬着
                        failure = {"error": "execution_error", "type": type(exc).__name__}
                        break
        # 飞书报错之后也提交：令牌刷新、授权失效这些状态变化要留下（与 MCP 入口同一口径）。
        # 提交本身失败（会话已处于失败事务）也不能让单悬着：回滚后照常记结果。
        try:
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            log.warning("assistant_approval_commit_failed", type=type(exc).__name__)
    return results, failure


def _outcome(rows: list[AssistantApproval]) -> str:
    """投回与返回用的结局：单独成单的是它的状态，合单统一记 `settled`（各项的结局在各项上）。"""
    return rows[0].status if len(rows) == 1 else "settled"


def _set(
    rows: list[AssistantApproval], status: str, code: str | None = None, result: Any = None
) -> None:
    for row in rows:
        row.status, row.error_code = status, code
        if result is not None:
            row.result_enc = service.bounded_result(result)
        if status == "executed":
            row.executed_at = utcnow()


async def _close(
    session: AsyncSession,
    row: AssistantApproval,
    bot: Bot,
    status: str,
    code: str | None = None,
    result: Any = None,
) -> str:
    """单独成单的一行有了结论：记下、投回并提交（调度与代理写操作的执行也用它）。"""
    _set([row], status, code, result)
    await service.deliver(session, row, bot)
    await session.commit()
    return status


async def _settle(
    session: AsyncSession,
    head: AssistantApproval,
    rows: list[AssistantApproval],
    bot: Bot,
) -> str:
    """各项已有结论：投回一轮（合单只投一轮）并提交。"""
    outcome = _outcome(rows)
    await service.deliver(session, head, bot, status=outcome)
    await session.commit()
    return outcome


async def _report(
    cfg: tower.Config | None, approval: str | None, status: str, code: str | None
) -> None:
    """告诉 Tower 这张单最后有没有执行；报不上只记日志，不影响本地结果。"""
    if cfg is None or not approval:
        return
    try:
        await tower.report(cfg, approval, "executed" if status == "executed" else "failed", code)
    except tower.TowerError as exc:
        log.warning("assistant_approval_report_failed", code=exc.code)


def _verdict(todo: list[AssistantApproval]) -> tuple[str, str | None]:
    """上报给 Tower 的结局：批准的各项全部执行了才算 executed，否则是第一项没执行的原因。"""
    for row in todo:
        if row.status != "executed":
            return "failed", "stale" if row.status == "stale" else row.error_code
    return "executed", None


def request_body(rows: list[AssistantApproval]) -> bytes:
    """审批系统批准的那一份请求体：单独成单是这一项，合单是全部各项（含已撤回的），按单上的顺序。"""
    if len(rows) == 1 and rows[0].batch_id is None:
        return tower.request_body(rows[0].tool, calls_of(rows[0]))
    return tower.batch_body([(row.tool, calls_of(row)) for row in rows])


async def run(
    factory: async_sessionmaker[AsyncSession],
    cipher: Cipher,
    approval_id: uuid.UUID,
    task: Task,
    cfg: tower.Config | None,
    files: FileRelay | None = None,
) -> str:
    """执行一张已批准的单，返回它最后的状态（合单返回 `settled`，各项的结局记在各项上）。

    合单执行批准时仍在待批（没撤回）的各项，与确认卡同一口径：先整单复查（会话、锚点、目标、
    附件），任何一项不通过整单都不执行，没通过的记各自的原因、其余记 `refused_with_batch`；
    全部通过才按单上的顺序执行，某一项失败就停下，后面的项记 `skipped_after_failure`，前面
    已执行的不撤回。

    Args:
        approval_id: 单独成单的那一行，或合单的第一项
        files: 文件中转；带文件的单要用它把附件交给原工具，没有时这类单记为失败

    Raises:
        Retry: 取附件、取令牌或核销时对方不可用；审批单保持 approved，没有任何副作用
    """
    async with factory() as session:
        row = await session.get(
            AssistantApproval, approval_id, with_for_update=True, populate_existing=True
        )
        if row is None:
            return "missing"
        rows = await service.members(session, row)
        todo = [item for item in rows if item.status == "approved"]
        if not todo:
            return row.status if row.batch_id is None else _outcome(rows)
        await sealed.guard_logs(session, row.bot_id)
        bot = await session.get(Bot, row.bot_id)
        approval = row.approval_id
        locked = any(sealed.is_locked(item) for item in rows)
        if bot is None or not bot.enabled or locked:
            # 助理已停用或数据密钥已销毁：参数读不出来，无法执行，也没有会话可以投回。
            code = sealed.LOCKED_CODE if locked else "assistant_inactive"
            _set(todo, "failed", code)
            await session.commit()
            await _report(cfg, approval, "failed", code)
            return "failed" if len(rows) == 1 else _outcome(rows)
        if any(item.execute_started_at is not None for item in todo):
            # 上一次已经开始调飞书、没等到结果：不知道是否生效，绝不再执行一次。
            _set(todo, "failed", "execution_interrupted")
            status = await _settle(session, row, rows, bot)
            await _report(cfg, approval, "failed", "execution_interrupted")
            return status
        scope = await owner_scope(session, bot, task)
        if scope is None or cfg is None or not approval:
            code = "authorization_required" if scope is None else service.UNAVAILABLE
            _set(todo, "failed", code)
            status = await _settle(session, row, rows, bot)
            await _report(cfg, approval, "failed", code)
            return status
        try:
            for item in todo:
                try:
                    await check_calls(session, cipher, scope, item.tool, calls_of(item))
                except checks.Rejected as exc:
                    _set([item], "stale", exc.code)
        except PersonalError as exc:
            _set(todo, "failed", exc.code)
            status = await _settle(session, row, rows, bot)
            await _report(cfg, approval, "failed", exc.code)
            return status
        # 先取回附件再核销：附件有问题时不白白用掉这次批准的令牌。
        blobs: dict[str, bytes] = {}
        for item in todo:
            if item.status != "approved":
                continue
            try:
                await fetch(files, calls_of(item), blobs)
            except Unrunnable as exc:
                # 被清理掉了，或桶里的内容不是提交时的那一份：这一项不执行。
                _set([item], "failed", exc.code)
        runnable = [item for item in todo if item.status == "approved"]
        if len(runnable) < len(todo):
            # 合单与确认卡同一口径：整单复查，任何一项不通过（会话变了、附件取不回或被改动）
            # 整单都不执行；没通过的那几项记各自的原因，其余记 `refused_with_batch`。
            _set(runnable, "failed", service.REFUSED)
            status = await _settle(session, row, rows, bot)
            await _report(cfg, approval, *_verdict(todo))
            return status
        head = await service.head_of(session, row)
        req_hash = tower.request_hash(cfg, head.external_id, request_body(rows), bot.bot_key)
        try:
            redeemed = await tower.redeem(
                cfg, await tower.token(cfg, approval), req_hash, bot.bot_key
            )
        except tower.TowerError as exc:
            if exc.unavailable:
                raise Retry from None
            # 令牌已用、已过期、请求对不上：这次批准作废，不执行。
            _set(runnable, "failed", exc.code)
            status = await _settle(session, row, rows, bot)
            await _report(cfg, approval, "failed", exc.code)
            return status
        if redeemed != approval:
            _set(runnable, "failed", "approval_mismatch")
            status = await _settle(session, row, rows, bot)
            await _report(cfg, approval, "failed", "approval_mismatch")
            return status
        started = utcnow()
        approved = {item.id for item in todo}
        plan = [(item.id, item.tool, calls_of(item)) for item in runnable]
        for item in runnable:
            item.execute_started_at = started
        bot_id = bot.id
        await session.commit()

    outcomes: dict[uuid.UUID, tuple[list[dict[str, Any]], dict[str, Any] | None] | None] = {}
    stopped = False
    for item_id, tool, calls in plan:
        if stopped:
            outcomes[item_id] = None
            continue
        results, failure = await perform(factory, cipher, bot_id, task, tool, calls, blobs, files)
        outcomes[item_id] = (results, failure)
        stopped = failure is not None

    async with factory() as session:
        row = await session.get(
            AssistantApproval, approval_id, with_for_update=True, populate_existing=True
        )
        bot = await session.get(Bot, bot_id)
        if row is None or bot is None:
            return "missing"
        rows = await service.members(session, row)
        mine = [item for item in rows if item.id in outcomes and item.status == "approved"]
        if not mine:
            # 别处（例如超时清理）已经给了结论：不再记一次，也不再投回、上报。
            return row.status if row.batch_id is None else _outcome(rows)
        for item in mine:
            done = outcomes[item.id]
            if done is None:
                _set([item], "failed", service.SKIPPED)
            elif done[1] is not None:
                code = str(done[1].get("error") or "failed")
                _set([item], "failed", code, {"done": done[0], "failure": done[1]})
            else:
                _set([item], "executed", None, done[0])
        status = await _settle(session, row, rows, bot)
    await _report(cfg, approval, *_verdict([item for item in rows if item.id in approved]))
    return status


async def give_up(
    factory: async_sessionmaker[AsyncSession],
    approval_id: uuid.UUID,
    cfg: tower.Config | None,
    code: str = "tower_unavailable",
) -> str:
    """审批系统或临时桶一直连不上：这张单记为失败并告诉助理。什么都没有执行过，可以重新提交。"""
    async with factory() as session:
        row = await session.get(
            AssistantApproval, approval_id, with_for_update=True, populate_existing=True
        )
        if row is None:
            return "missing"
        rows = await service.members(session, row)
        todo = [item for item in rows if item.status == "approved"]
        if not todo:
            return row.status if row.batch_id is None else _outcome(rows)
        bot = await session.get(Bot, row.bot_id)
        approval = row.approval_id
        _set(todo, "failed", code)
        if bot is None:
            await session.commit()
            status = "failed" if len(rows) == 1 else _outcome(rows)
        else:
            status = await _settle(session, row, rows, bot)
    await _report(cfg, approval, "failed", code)
    return status


async def recover_stuck(session: AsyncSession, now: datetime, cfg: tower.Config | None) -> int:
    """批准之后一直没有结论、也没有在途执行任务的单：记为失败并告诉助理与 Tower。

    已记下开始执行的是 `execution_interrupted`（不知道是否生效）；没有记下的是
    `execution_not_started`（什么都没执行，可以重新提交）。合单的执行任务挂在第一项上，
    各项一起收口，只投回一轮。

    参考时间取 `execute_started_at`，没有则取 `decided_at`（批准落库的时间）。不提交，由调用方
    的事务负责。返回关掉的项数。
    """
    since = now - STUCK_AFTER
    key = func.coalesce(AssistantApproval.batch_id, AssistantApproval.id)
    alive = exists().where(
        Task.dedupe_key == func.concat("assistant-approval:", key, ":execute"),
        Task.status.in_(tasks.OPEN),
    )
    ids = list(
        await session.scalars(
            select(AssistantApproval.id).where(
                AssistantApproval.status == "approved",
                func.coalesce(AssistantApproval.execute_started_at, AssistantApproval.decided_at)
                < since,
                ~alive,
            )
        )
    )
    closed = 0
    settled: set[uuid.UUID] = set()
    for approval_id in ids:
        row = await session.get(
            AssistantApproval,
            approval_id,
            with_for_update={"skip_locked": True},
            populate_existing=True,
        )
        if row is None or row.status != "approved" or (row.batch_id or row.id) in settled:
            continue
        await sealed.guard_logs(session, row.bot_id)
        rows = await service.members(session, row)
        todo = [item for item in rows if item.status == "approved"]
        settled.add(row.batch_id or row.id)
        bot = await session.get(Bot, row.bot_id)
        approval = row.approval_id
        locked = any(sealed.is_locked(item) for item in rows)
        if bot is None or not bot.enabled or locked:
            code = sealed.LOCKED_CODE if locked else "assistant_inactive"
            _set(todo, "failed", code)
        else:
            # 没记下「开始执行」就说明一次飞书调用都没发过：与「调到一半被杀」分开说。
            code = (
                "execution_interrupted"
                if any(item.execute_started_at is not None for item in todo)
                else "execution_not_started"
            )
            for item in todo:
                _set(
                    [item],
                    "failed",
                    "execution_interrupted"
                    if item.execute_started_at is not None
                    else "execution_not_started",
                )
            await service.deliver(
                session, await service.head_of(session, row), bot, status=_outcome(rows)
            )
        await _report(cfg, approval, "failed", code)
        closed += len(todo)
    return closed

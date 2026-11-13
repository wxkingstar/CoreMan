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


async def _close(
    session: AsyncSession,
    row: AssistantApproval,
    bot: Bot,
    status: str,
    code: str | None = None,
    result: Any = None,
) -> str:
    row.status, row.error_code = status, code
    if result is not None:
        row.result_enc = service.bounded_result(result)
    if status == "executed":
        row.executed_at = utcnow()
    await service.deliver(session, row, bot)
    await session.commit()
    return status


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


async def run(
    factory: async_sessionmaker[AsyncSession],
    cipher: Cipher,
    approval_id: uuid.UUID,
    task: Task,
    cfg: tower.Config | None,
    files: FileRelay | None = None,
) -> str:
    """执行一张已批准的单，返回它最后的状态。

    Args:
        files: 文件中转；带文件的单要用它把附件交给原工具，没有时这类单记为失败

    Raises:
        Retry: 取附件、取令牌或核销时对方不可用；审批单保持 approved，没有任何副作用
    """
    async with factory() as session:
        row = await session.get(
            AssistantApproval, approval_id, with_for_update=True, populate_existing=True
        )
        if row is None or row.status != "approved":
            return row.status if row is not None else "missing"
        await sealed.guard_logs(session, row.bot_id)
        bot = await session.get(Bot, row.bot_id)
        approval, tool = row.approval_id, row.tool
        if bot is None or not bot.enabled or sealed.is_locked(row):
            # 助理已停用或数据密钥已销毁：参数读不出来，无法执行，也没有会话可以投回。
            code = sealed.LOCKED_CODE if sealed.is_locked(row) else "assistant_inactive"
            row.status, row.error_code = "failed", code
            await session.commit()
            await _report(cfg, approval, "failed", code)
            return "failed"
        if row.execute_started_at is not None:
            # 上一次已经开始调飞书、没等到结果：不知道是否生效，绝不再执行一次。
            status = await _close(session, row, bot, "failed", "execution_interrupted")
            await _report(cfg, approval, status, "execution_interrupted")
            return status
        scope = await owner_scope(session, bot, task)
        if scope is None or cfg is None or not approval:
            code = "authorization_required" if scope is None else service.UNAVAILABLE
            status = await _close(session, row, bot, "failed", code)
            await _report(cfg, approval, status, code)
            return status
        calls = list((row.args_enc or {}).get("calls") or [])
        reader = Call(session, cipher, scope)
        try:
            for one in calls:
                await checks.check_call(reader, tool, one, owner_open_id=scope.open_id)
        except checks.Rejected as exc:
            status = await _close(session, row, bot, "stale", exc.code)
            await _report(cfg, approval, status, "stale")
            return status
        except PersonalError as exc:
            status = await _close(session, row, bot, "failed", exc.code)
            await _report(cfg, approval, status, exc.code)
            return status
        # 先取回附件再核销：附件有问题时不白白用掉这次批准的令牌。
        blobs: dict[str, bytes] = {}
        refs = attachments.references(calls)
        store = attachments.store(files.settings) if files is not None else None
        if refs and store is None:
            status = await _close(session, row, bot, "failed", "attachments_not_supported")
            await _report(cfg, approval, status, "attachments_not_supported")
            return status
        try:
            for ref in refs:
                assert store is not None
                blobs[ref["key"]] = await attachments.get(store, ref)
        except attachments.Unavailable:
            raise Retry("attachment_store_unavailable") from None
        except (attachments.Missing, attachments.Changed) as exc:
            # 被清理掉了，或桶里的内容不是提交时的那一份：不执行。
            missing = isinstance(exc, attachments.Missing)
            code = "attachment_missing" if missing else "attachment_changed"
            status = await _close(session, row, bot, "failed", code)
            await _report(cfg, approval, status, code)
            return status
        req_hash = tower.request_hash(
            cfg, row.external_id, tower.request_body(tool, calls), bot.bot_key
        )
        try:
            redeemed = await tower.redeem(
                cfg, await tower.token(cfg, approval), req_hash, bot.bot_key
            )
        except tower.TowerError as exc:
            if exc.unavailable:
                raise Retry from None
            # 令牌已用、已过期、请求对不上：这次批准作废，不执行。
            status = await _close(session, row, bot, "failed", exc.code)
            await _report(cfg, approval, status, exc.code)
            return status
        if redeemed != approval:
            status = await _close(session, row, bot, "failed", "approval_mismatch")
            await _report(cfg, approval, status, "approval_mismatch")
            return status
        row.execute_started_at = utcnow()
        bot_id = bot.id
        await session.commit()

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

    async with factory() as session:
        row = await session.get(
            AssistantApproval, approval_id, with_for_update=True, populate_existing=True
        )
        bot = await session.get(Bot, bot_id)
        if row is None or bot is None:
            return "missing"
        if row.status != "approved":
            # 别处（例如超时清理）已经给了结论：不再记一次，也不再投回、上报。
            return row.status
        outcome: str | None = None
        if failure is not None:
            outcome = str(failure.get("error") or "failed")
            status = await _close(
                session, row, bot, "failed", outcome, {"done": results, "failure": failure}
            )
        else:
            status = await _close(session, row, bot, "executed", None, results)
    await _report(cfg, approval, status, outcome)
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
        if row is None or row.status != "approved":
            return row.status if row is not None else "missing"
        bot = await session.get(Bot, row.bot_id)
        approval = row.approval_id
        if bot is None:
            row.status, row.error_code = "failed", code
            await session.commit()
            status = "failed"
        else:
            status = await _close(session, row, bot, "failed", code)
    await _report(cfg, approval, status, code)
    return status


async def recover_stuck(session: AsyncSession, now: datetime, cfg: tower.Config | None) -> int:
    """批准之后一直没有结论、也没有在途执行任务的单：记为失败并告诉助理与 Tower。

    已记下开始执行的是 `execution_interrupted`（不知道是否生效）；没有记下的是
    `execution_not_started`（什么都没执行，可以重新提交）。

    参考时间取 `execute_started_at`，没有则取 `decided_at`（批准落库的时间）。不提交，由调用方
    的事务负责。返回关掉的张数。
    """
    since = now - STUCK_AFTER
    alive = exists().where(
        Task.dedupe_key == func.concat("assistant-approval:", AssistantApproval.id, ":execute"),
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
    for approval_id in ids:
        row = await session.get(
            AssistantApproval,
            approval_id,
            with_for_update={"skip_locked": True},
            populate_existing=True,
        )
        if row is None or row.status != "approved":
            continue
        await sealed.guard_logs(session, row.bot_id)
        bot = await session.get(Bot, row.bot_id)
        approval = row.approval_id
        if bot is None or not bot.enabled or sealed.is_locked(row):
            code = sealed.LOCKED_CODE if sealed.is_locked(row) else "assistant_inactive"
            row.status, row.error_code = "failed", code
        else:
            # 没记下「开始执行」就说明一次飞书调用都没发过：与「调到一半被杀」分开说。
            code = (
                "execution_interrupted"
                if row.execute_started_at is not None
                else "execution_not_started"
            )
            row.status, row.error_code = "failed", code
            await service.deliver(session, row, bot)
        await _report(cfg, approval, "failed", code)
        closed += 1
    return closed

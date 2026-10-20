"""Bounded workspace preparation, durable migration fencing and recovery snapshots."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import uuid
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.workspace import reserve_workspace
from coreman.core.chat import sessions
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, RelayServer, RuntimeNode
from coreman.core.errors import ApiError
from coreman.core.knowledge import memory_transfer
from coreman.core.logging import get_logger
from coreman.core.relay.agent_client import AgentError, call_agent
from coreman.core.timeutils import utcnow

log = get_logger(__name__)

WorkspaceMode = Literal["copy", "existing"]
# 旧运行时只认 256KB 分块；新运行时在 ping 里报 workspace_chunk。
CHUNK_SIZE = 256 * 1024
MAX_CHUNK = 2 * 1024 * 1024
MAX_FILES = 50000
SESSION_COPY_TIMEOUT = 5 * 60


def failure_message(exc: BaseException) -> str:
    """换机失败给人看的原因：节点报的固定提示原样带上，不再只说「迁移失败」。"""
    if isinstance(exc, ApiError):
        return exc.message
    if isinstance(exc, TimeoutError):
        return "迁移超过 25 分钟未完成，原绑定已保留；请稍后重试或在管理台清理大文件后重试"
    if isinstance(exc, AgentError) and exc.detail:
        return f"迁移失败：{exc.detail}"
    if isinstance(exc, AgentError) and exc.code in {"execution_failed", "connection_lost"}:
        return "迁移中运行时连接中断，原绑定已保留，请确认两端运行时在线后重试"
    return "文件或记忆迁移失败，请检查运行时后重试"


def instruction_content(bot: Bot) -> str:
    return (
        f"# {bot.name}\n\n## 职责\n{bot.description or '协助团队完成工作任务。'}\n\n"
        "## 工作约定\n在当前员工工作目录中保存工作文件。遵循平台提供的任务说明和授权范围。\n"
        "不要把口令、令牌和环境变量写入文件或 Git。\n\n"
        "## 记忆\n使用平台提供的持久记忆；换机时由平台回收并部署记忆。\n"
    )


def claimable(info: dict[str, Any]) -> bool:
    """已有目录能否「使用已有目录」：本员工的，或没有任何员工的所有权标记（如另一套机器人系统
    在用的目录，接管时原有文件与指令布局不动，只写入标记）。旧运行时不报告 marked，按有标记处理。"""
    return bool(info.get("owned")) or info.get("marked", True) is False


async def require_workspace(relay: RelayServer, cipher: Cipher | None) -> dict[str, Any]:
    result = await call_agent(relay, cipher, "ping")
    if result.get("workspace_protocol") != 1:
        raise ApiError(409, 409, "请先升级运行时，以支持工作目录管理")
    return result


async def shared_directory(
    bot: Bot,
    source: RelayServer,
    target: RelayServer,
    directory: str,
    cipher: Cipher | None,
) -> bool:
    """源节点的当前目录与目标节点的 `directory` 是不是共享盘上的同一份：源节点在当前目录建随机名
    文件，目标节点在 `directory` 找得到就是。不要求两边路径相同：同一块盘在各节点可以挂在不同路径。

    任一端不支持探测按不共享处理，沿用复制；探测本身出错则抛出，调用方不能当成不共享去复制——
    共享盘上的整目录替换会删掉不随迁移的 .env、依赖目录和开发模式 worktree。
    """
    for relay in (source, target):
        if (await call_agent(relay, cipher, "ping")).get("workspace_probe") != 1:
            return False
    token, owner = uuid.uuid4().hex, str(bot.id)
    here = {"working_dir": bot.working_dir, "bot_id": owner, "token": token}
    await call_agent(source, cipher, "workspace-probe", {**here, "action": "create"})
    try:
        result = await call_agent(
            target,
            cipher,
            "workspace-probe",
            {"working_dir": directory, "bot_id": owner, "token": token, "action": "check"},
        )
    finally:
        # 目标只查不删，由建文件的源节点删；删不掉不影响结论。
        with contextlib.suppress(AgentError, TimeoutError):
            await call_agent(source, cipher, "workspace-probe", {**here, "action": "remove"})
    return result.get("found") is True


async def initialize_workspace(session: AsyncSession, bot: Bot, cipher: Cipher | None) -> None:
    if not bot.relay_server_id:
        bot.workspace_state, bot.workspace_phase = "pending", "等待分配运行时"
        return
    relay = await session.get(RelayServer, bot.relay_server_id)
    if relay is None:
        raise ApiError(422, 422, "运行时不存在")
    await memory_transfer.ensure_idle(session, bot)
    bot.workspace_state, bot.workspace_phase = "initializing", "初始化工作说明"
    try:
        await require_workspace(relay, cipher)
        await call_agent(
            relay,
            cipher,
            "workspace-init",
            {
                "working_dir": bot.working_dir,
                "bot_id": str(bot.id),
                "content": instruction_content(bot),
            },
        )
    except (ApiError, AgentError, TimeoutError) as exc:
        bot.workspace_state, bot.workspace_error = "failed", str(exc)
        if isinstance(exc, AgentError) and exc.code == "instructions_conflict":
            bot.workspace_error = "AGENTS.md 与 CLAUDE.md 内容不一致，请在文件管理中合并内容后重试"
            bot.workspace_phase = "工作说明需要合并"
            raise ApiError(409, 409, bot.workspace_error) from exc
        bot.workspace_phase = "初始化失败，可重试"
        if isinstance(exc, ApiError):
            raise
        raise ApiError(502, 502, "工作目录初始化失败，请检查运行时后重试") from exc
    bot.workspace_target_relay_id, bot.workspace_target_dir = None, None
    bot.workspace_state, bot.workspace_error = "ready", None
    bot.workspace_phase, bot.workspace_operation_id, bot.workspace_deadline = None, None, None


async def target_path(
    session: AsyncSession,
    bot: Bot,
    target: RelayServer,
    requested: str | None = None,
) -> str:
    node = await session.get(RuntimeNode, target.runtime_node_id)
    if node is None:
        raise ApiError(422, 422, "目标运行时未注册")
    relative = PurePosixPath(bot.bot_key)
    old = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
    source = await session.get(RuntimeNode, old.runtime_node_id) if old else None
    if source and PurePosixPath(bot.working_dir).is_relative_to(source.workspace_root):
        relative = PurePosixPath(bot.working_dir).relative_to(source.workspace_root)
    value = requested or str(PurePosixPath(node.workspace_root) / relative)
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or "\x00" in value:
        raise ApiError(422, 422, "目标目录须为无上级跳转的绝对路径")
    await reserve_workspace(session, target.id, str(path), bot_id=bot.id)
    return str(path)


def revision(repo: dict[str, Any] | None) -> dict[str, Any] | None:
    """仓库描述里决定克隆结果的部分；bundle 的字节每次打包可能不同，不参与比较。"""
    return {k: v for k, v in repo.items() if k != "bundle"} if repo else None


async def transfer_chunks(
    steps: list[tuple[dict[str, Any], int]],
    read: Any,
    write: Any,
) -> None:
    """按块从源读、往目标写；写当前块的同时预读下一块，两端各自串行。"""
    if not steps:
        return
    pending = asyncio.ensure_future(read(*steps[0]))
    try:
        for index, step in enumerate(steps):
            chunk = await pending
            if index + 1 < len(steps):
                pending = asyncio.ensure_future(read(*steps[index + 1]))
            await write(*step, chunk)
    finally:
        if not pending.done():
            pending.cancel()


async def copy_files(
    bot: Bot,
    source: RelayServer,
    target: RelayServer,
    directory: str,
    cipher: Cipher | None,
    operation_id: uuid.UUID,
    *,
    occupied: bool,
    capability: dict[str, Any],
) -> None:
    """用源目录完全覆盖目标目录（含 .git）；目标原有内容整体替换掉。

    两端都支持时，有远端的仓库由目标节点重新克隆，只复制改动、未跟踪文件和未推送的提交；
    克隆不了的仓库（没有凭证、远端不可达）退回整仓复制。
    """
    src = {"working_dir": bot.working_dir, "bot_id": str(bot.id)}
    dst: dict[str, Any] = {
        "working_dir": directory,
        "bot_id": str(bot.id),
        "transfer_id": str(operation_id),
    }
    source_capability = await require_workspace(source, cipher)
    git_aware = bool(source_capability.get("workspace_git") and capability.get("workspace_git"))
    chunk_size = min(
        int(source_capability.get("workspace_chunk") or CHUNK_SIZE),
        int(capability.get("workspace_chunk") or CHUNK_SIZE),
        MAX_CHUNK,
    )
    export = await call_agent(source, cipher, "workspace-export", {**src, "git": git_aware})
    cloned: list[dict[str, Any]] = []
    failed: list[str] = []
    for repo in export.get("repos") or []:
        try:
            await call_agent(target, cipher, "workspace-clone", {**dst, "repo": repo})
        except AgentError as exc:
            log.warning("workspace_clone_failed", bot_id=str(bot.id), detail=exc.detail)
            failed.append(repo["path"])
        else:
            cloned.append(repo)
    if failed:
        export = await call_agent(
            source, cipher, "workspace-export", {**src, "git": True, "expand": failed}
        )
        again = {repo["path"]: repo for repo in export.get("repos") or []}
        if any(revision(again.get(repo["path"])) != revision(repo) for repo in cloned):
            raise ApiError(409, 409, "源文件在复制时发生变化，请重试")
    manifest = export.get("manifest")
    if not isinstance(manifest, list) or len(manifest) > MAX_FILES:
        raise ApiError(502, 502, "源文件清单无效")
    if not capability.get("workspace_replace") and (
        occupied or any(".git" in PurePosixPath(entry["path"]).parts for entry in manifest)
    ):
        raise ApiError(409, 409, "目标运行时版本较旧，不能覆盖已有目录或复制 .git，请先升级运行时")
    await call_agent(target, cipher, "workspace-import", {**dst, "manifest": manifest})
    steps: list[tuple[dict[str, Any], int]] = []
    for entry in manifest:
        if "link" in entry:
            continue
        size = entry["size"]
        if not isinstance(size, int) or size < 0:
            raise ApiError(502, 502, "源文件大小无效")
        # Empty files also need creation in staging.
        steps += [(entry, offset) for offset in range(0, max(size, 1), chunk_size)]
    digests: dict[str, Any] = {}

    async def read(entry: dict[str, Any], offset: int) -> dict[str, Any]:
        return await call_agent(
            source,
            cipher,
            "workspace-read",
            {
                **src,
                "transfer_id": str(operation_id),
                "path": entry["path"],
                "offset": offset,
                "length": chunk_size,
            },
        )

    async def write(entry: dict[str, Any], offset: int, chunk: dict[str, Any]) -> None:
        raw = base64.b64decode(chunk["data"], validate=True)
        size = entry["size"]
        if len(raw) != min(chunk_size, size - offset) or chunk["hash"] != entry["hash"]:
            raise ApiError(409, 409, "源文件在复制时发生变化，请重试")
        digest = digests.setdefault(entry["path"], hashlib.sha256())
        digest.update(raw)
        await call_agent(
            target,
            cipher,
            "workspace-import",
            {**dst, "path": entry["path"], "offset": offset, "data": chunk["data"]},
        )
        if offset + chunk_size >= size and digest.hexdigest() != entry["hash"]:
            raise ApiError(502, 502, "文件传输校验失败")

    await transfer_chunks(steps, read, write)
    await call_agent(
        target,
        cipher,
        "workspace-finish",
        {
            **dst,
            "manifest": manifest,
            "content": instruction_content(bot),
            "replace": True,
            "repos": cloned,
        },
    )


async def copy_sessions(
    session: AsyncSession,
    bot: Bot,
    source: RelayServer,
    target: RelayServer,
    source_dir: str,
    target_dir: str,
    cipher: Cipher | None,
) -> set[uuid.UUID]:
    """把 Claude 会话文件搬到目标运行时，返回搬成功的 relay 会话；搬不了的由调用方改为文本续接。

    只做 Claude → Claude：Codex 的续聊还依赖本机状态库，单搬文件续不上。尽力而为，失败不影响换机。
    """
    if source.model_provider != "claude" or target.model_provider != "claude":
        return set()
    rows = [row for row in await sessions.list_for_bot(session, bot.id) if row.backend == "claude"]
    if not rows:
        return set()
    copied: set[uuid.UUID] = set()
    transfer_id = str(uuid.uuid4())
    try:
        async with asyncio.timeout(SESSION_COPY_TIMEOUT):
            for relay in (source, target):
                if (await call_agent(relay, cipher, "ping")).get("session_transfer") != 1:
                    return set()
            listing = await call_agent(
                source,
                cipher,
                "session-export",
                {
                    "working_dir": source_dir,
                    "bot_id": str(bot.id),
                    "sessions": [str(row.relay_session_id) for row in rows],
                },
            )
            grouped: dict[str, list[dict[str, Any]]] = {}
            for item in listing.get("files") or []:
                sid = str(item["path"]).split("/", 1)[0].removesuffix(".jsonl")
                grouped.setdefault(sid, []).append(item)
            for sid, items in grouped.items():
                try:
                    await copy_session_files(
                        bot, source, target, source_dir, target_dir, cipher, transfer_id, items
                    )
                except (AgentError, ApiError, ValueError, KeyError) as exc:
                    log.warning(
                        "session_copy_failed",
                        bot_id=str(bot.id),
                        error=type(exc).__name__,
                        detail=getattr(exc, "detail", None),
                    )
                    continue
                copied.add(uuid.UUID(sid))
    except (AgentError, ApiError, TimeoutError, ValueError, KeyError) as exc:
        log.warning("session_copy_skipped", bot_id=str(bot.id), error=type(exc).__name__)
    return copied


async def copy_session_files(
    bot: Bot,
    source: RelayServer,
    target: RelayServer,
    source_dir: str,
    target_dir: str,
    cipher: Cipher | None,
    transfer_id: str,
    items: list[dict[str, Any]],
) -> None:
    steps = [
        (item, offset)
        for item in items
        for offset in range(0, max(int(item["size"]), 1), MAX_CHUNK)
    ]

    async def read(item: dict[str, Any], offset: int) -> dict[str, Any]:
        return await call_agent(
            source,
            cipher,
            "session-read",
            {
                "working_dir": source_dir,
                "bot_id": str(bot.id),
                "area": item["area"],
                "path": item["path"],
                "offset": offset,
                "length": MAX_CHUNK,
            },
        )

    async def write(item: dict[str, Any], offset: int, chunk: dict[str, Any]) -> None:
        if chunk["hash"] != item["hash"]:
            raise ApiError(409, 409, "会话文件在复制时发生变化")
        await call_agent(
            target,
            cipher,
            "session-import",
            {
                "working_dir": target_dir,
                "bot_id": str(bot.id),
                "transfer_id": transfer_id,
                "area": item["area"],
                "path": item["path"],
                "offset": offset,
                "size": item["size"],
                "hash": item["hash"],
                "data": chunk["data"],
            },
        )

    await transfer_chunks(steps, read, write)


async def prepare_switch(
    session: AsyncSession,
    bot: Bot,
    target: RelayServer,
    cipher: Cipher | None,
    *,
    mode: WorkspaceMode = "copy",
    directory: str,
    allow_stored_memory: bool = False,
) -> str:
    """Caller owns bot row lock. Intermediate commits persist fence and recovered memory.

    Final binding and ready state remain caller-transactional. A process crash leaves a
    durable fence; retry is allowed after the 30 minute deadline (work has a 25m timeout).
    """
    if bot.workspace_state in {"migrating", "initializing", "busy"}:
        if bot.workspace_deadline is None or bot.workspace_deadline > utcnow():
            raise ApiError(409, 409, "工作目录操作正在进行，请等待完成")
    await memory_transfer.ensure_idle(session, bot)
    old = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
    same_directory = bool(
        old and old.runtime_node_id == target.runtime_node_id and directory == bot.working_dir
    )
    operation_id = uuid.uuid4()
    bot.workspace_state, bot.workspace_error = "migrating", None
    bot.workspace_phase = "检查目标目录"
    bot.workspace_operation_id = operation_id
    bot.workspace_target_relay_id, bot.workspace_target_dir = target.id, directory
    bot.workspace_deadline = utcnow() + timedelta(minutes=30)
    await session.commit()
    try:
        async with asyncio.timeout(25 * 60):
            capability = await require_workspace(target, cipher)
            info = await call_agent(
                target,
                cipher,
                "workspace-info",
                {
                    "working_dir": directory,
                    "bot_id": str(bot.id),
                },
            )
            occupied = bool(info.get("exists") and not info.get("empty"))
            if not same_directory:
                if mode == "existing":
                    if not info.get("exists") or not claimable(info):
                        raise ApiError(409, 409, "目标目录不存在或属于其他员工，请选择复制当前文件")
                elif occupied and not claimable(info):
                    raise ApiError(409, 409, "目标目录属于其他员工，不能覆盖，请选择其他目录")
                elif occupied and not old:
                    raise ApiError(409, 409, "目标目录已有内容，请明确复用本员工目录或选择新的目录")
            # 复制会整体替换非空的目标目录；它可能就是共享盘上的同一份（路径可以不同，归属标记也可能
            # 被 git clean -x 删掉），先探测，是就不复制。限流卡片的自动切换默认就是复制。
            shared = False
            if not same_directory and mode == "copy" and old and occupied:
                try:
                    shared = await shared_directory(bot, old, target, directory, cipher)
                except AgentError as exc:
                    raise ApiError(
                        502,
                        502,
                        "无法确认目标目录是否与当前目录共享，为避免误覆盖已停止换机；"
                        "请确认两端运行时在线后重试",
                    ) from exc
            bot.workspace_phase = "回收最新记忆"
            await session.commit()
            status = await memory_transfer.recover(
                session,
                bot,
                cipher,
                allow_stored=allow_stored_memory and mode != "copy",
            )
            bot.workspace_phase = "共享存储，跳过文件复制" if shared else "准备目标工作文件"
            await session.commit()  # Snapshot survives subsequent transfer failure.
            if same_directory or shared or mode == "existing":
                await call_agent(
                    target,
                    cipher,
                    "workspace-init",
                    {
                        "working_dir": directory,
                        "bot_id": str(bot.id),
                        "content": instruction_content(bot),
                    },
                )
            elif old:
                await copy_files(
                    bot,
                    old,
                    target,
                    directory,
                    cipher,
                    operation_id,
                    occupied=occupied,
                    capability=capability,
                )
            else:
                await call_agent(
                    target,
                    cipher,
                    "workspace-init",
                    {
                        "working_dir": directory,
                        "bot_id": str(bot.id),
                        "content": instruction_content(bot),
                    },
                )
            bot.workspace_phase = "部署并校验记忆"
            await session.commit()
            await memory_transfer.deploy(
                session, bot, target, cipher, target_directory=directory, overwrite=True
            )
            # Reacquire lock before final binding, fail closed on stale operation identity.
            await session.refresh(bot, with_for_update=True)
            if bot.workspace_operation_id != operation_id:
                raise ApiError(409, 409, "迁移状态已变化，请刷新后重试")
            await reserve_workspace(session, target.id, directory, bot_id=bot.id)
            bot.workspace_phase = "完成切换"
            return status
    except (ApiError, AgentError, TimeoutError, ValueError, KeyError, OSError) as exc:
        log.warning(
            "workspace_migration_failed",
            bot_id=str(bot.id),
            phase=bot.workspace_phase,
            error=type(exc).__name__,
            code=getattr(exc, "code", None),
            detail=getattr(exc, "detail", None),
        )
        await session.refresh(bot, with_for_update=True)
        if bot.workspace_operation_id != operation_id:
            raise ApiError(409, 409, "迁移状态已更新，请刷新后查看") from exc
        bot.workspace_state, bot.workspace_phase = "failed", "迁移失败，原绑定已保留"
        bot.workspace_error = failure_message(exc)
        bot.workspace_deadline = None
        await session.commit()
        if isinstance(exc, ApiError):
            raise
        raise ApiError(502, 502, bot.workspace_error) from exc

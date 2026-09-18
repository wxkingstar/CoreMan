"""Bounded workspace preparation, durable migration fencing and recovery snapshots."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.workspace import reserve_workspace
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, RelayServer, RuntimeNode
from coreman.core.errors import ApiError
from coreman.core.knowledge import memory_transfer
from coreman.core.relay.agent_client import AgentError, call_agent
from coreman.core.timeutils import utcnow

WorkspaceMode = Literal["copy", "git", "existing"]
CHUNK_SIZE = 256 * 1024


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


async def require_workspace(relay: RelayServer, cipher: Cipher | None) -> None:
    result = await call_agent(relay, cipher, "ping")
    if result.get("workspace_protocol") != 1:
        raise ApiError(409, 409, "请先升级运行时，以支持工作目录管理")


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


async def copy_files(
    bot: Bot,
    source: RelayServer,
    target: RelayServer,
    directory: str,
    cipher: Cipher | None,
    operation_id: uuid.UUID,
) -> None:
    src = {"working_dir": bot.working_dir, "bot_id": str(bot.id)}
    dst: dict[str, Any] = {
        "working_dir": directory,
        "bot_id": str(bot.id),
        "transfer_id": str(operation_id),
    }
    export = await call_agent(source, cipher, "workspace-export", src)
    manifest = export.get("manifest")
    if not isinstance(manifest, list) or len(manifest) > 10000:
        raise ApiError(502, 502, "源文件清单无效")
    await call_agent(target, cipher, "workspace-import", {**dst, "manifest": manifest})
    for entry in manifest:
        if "link" in entry:
            continue
        digest = hashlib.sha256()
        size = entry["size"]
        if not isinstance(size, int) or size < 0:
            raise ApiError(502, 502, "源文件大小无效")
        # Empty files also need creation in staging.
        for offset in range(0, max(size, 1), CHUNK_SIZE):
            chunk = await call_agent(
                source,
                cipher,
                "workspace-read",
                {
                    **src,
                    "path": entry["path"],
                    "offset": offset,
                    "length": CHUNK_SIZE,
                },
            )
            raw = base64.b64decode(chunk["data"], validate=True)
            if len(raw) != min(CHUNK_SIZE, size - offset) or chunk["hash"] != entry["hash"]:
                raise ApiError(409, 409, "源文件在复制时发生变化，请重试")
            digest.update(raw)
            await call_agent(
                target,
                cipher,
                "workspace-import",
                {
                    **dst,
                    "path": entry["path"],
                    "offset": offset,
                    "data": chunk["data"],
                },
            )
        if digest.hexdigest() != entry["hash"]:
            raise ApiError(502, 502, "文件传输校验失败")
    await call_agent(
        target,
        cipher,
        "workspace-finish",
        {
            **dst,
            "manifest": manifest,
            "content": instruction_content(bot),
        },
    )


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
            await require_workspace(target, cipher)
            info = await call_agent(
                target,
                cipher,
                "workspace-info",
                {
                    "working_dir": directory,
                    "bot_id": str(bot.id),
                },
            )
            if not same_directory:
                if mode == "existing":
                    if not info.get("exists") or not claimable(info):
                        raise ApiError(
                            409, 409, "目标目录不存在或属于其他员工，请选择复制或 Git 恢复"
                        )
                elif info.get("exists") and not info.get("empty"):
                    raise ApiError(409, 409, "目标目录已有内容，请明确复用本员工目录或选择新的目录")
            bot.workspace_phase = "回收最新记忆"
            await session.commit()
            status = await memory_transfer.recover(
                session,
                bot,
                cipher,
                allow_stored=allow_stored_memory and mode != "copy",
            )
            bot.workspace_phase = "准备目标工作文件"
            await session.commit()  # Snapshot survives subsequent transfer failure.
            if same_directory or mode == "existing":
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
            elif mode == "git":
                if not bot.git_url:
                    raise ApiError(422, 422, "请先配置 Git 备份地址")
                token = (
                    cipher.decrypt(bot.git_token_enc, "bots.git_token_enc")
                    if cipher and bot.git_token_enc
                    else ""
                )
                await call_agent(
                    target,
                    cipher,
                    "workspace-git-restore",
                    {
                        "working_dir": directory,
                        "bot_id": str(bot.id),
                        "git_url": bot.git_url,
                        "branch": bot.git_branch,
                        "git_access_token": token,
                        "content": instruction_content(bot),
                        "transfer_id": str(operation_id),
                    },
                )
            elif old:
                await require_workspace(old, cipher)
                await copy_files(bot, old, target, directory, cipher, operation_id)
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
            await memory_transfer.deploy(session, bot, target, cipher, target_directory=directory)
            # Reacquire lock before final binding, fail closed on stale operation identity.
            await session.refresh(bot, with_for_update=True)
            if bot.workspace_operation_id != operation_id:
                raise ApiError(409, 409, "迁移状态已变化，请刷新后重试")
            await reserve_workspace(session, target.id, directory, bot_id=bot.id)
            bot.workspace_phase = "完成切换"
            return status
    except (ApiError, AgentError, TimeoutError, ValueError, KeyError, OSError) as exc:
        await session.refresh(bot, with_for_update=True)
        if bot.workspace_operation_id != operation_id:
            raise ApiError(409, 409, "迁移状态已更新，请刷新后查看") from exc
        bot.workspace_state, bot.workspace_phase = "failed", "迁移失败，原绑定已保留"
        bot.workspace_error = (
            str(exc) if isinstance(exc, ApiError) else "文件或记忆迁移失败，请检查运行时后重试"
        )
        bot.workspace_deadline = None
        await session.commit()
        if isinstance(exc, ApiError):
            raise
        raise ApiError(502, 502, bot.workspace_error) from exc

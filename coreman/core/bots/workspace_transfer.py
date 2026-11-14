"""Bounded workspace preparation, durable migration fencing and recovery snapshots."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants.records import (
    ASSISTANT_NOT_ACTIVE_MESSAGE,
    inactive_assistant,
    is_assistant,
    unsupported,
)
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
# 不报 workspace_read_raw 的源运行时读文本块时还会附原文，JSON 转义最坏一个字节变六个：
# 512KB 的块 base64 加原文仍在 call_agent 的 4MB 响应上限内，2MB 的块会超出、换机失败。
LEGACY_READ_CHUNK = 512 * 1024
MAX_FILES = 50000
# 经平台中转时的总大小上限，与节点一致；直连失败后只有在这个范围内才退回中转。
MAX_TOTAL = 4 * 1024**3
# 局域网直连（两台节点在同一网络区）时放宽到 20 万个文件、32G，与节点一致。
DIRECT_MAX_FILES = 200000
# 直连拉取的进度轮询间隔；连续这么久没有新字节就判定卡住。
DIRECT_POLL_SECONDS = 1.0
DIRECT_STALL_SECONDS = 120
SESSION_COPY_TIMEOUT = 5 * 60
# 各会话的文件互不相干，并行搬；每个会话内部仍按块串行。
SESSION_COPY_CONCURRENCY = 4
# 会话随换机迁移所需的节点能力位，按后端区分。
SESSION_CAPABILITY = {
    "claude": "session_transfer",
    "codex": "session_transfer_codex",
    "opencode": "session_transfer_opencode",
}


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
        "## 记忆\n持久记忆在本目录的 `.coreman-memory/`（`MEMORY.md` 为索引），换运行时也保留。\n"
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

    任一端不支持探测、或探测本身出错都抛出，调用方不能当成不共享去复制——共享盘上的整目录
    替换会删掉不随迁移的 .env、依赖目录和开发模式 worktree。
    """
    for relay in (source, target):
        if (await call_agent(relay, cipher, "ping")).get("workspace_probe") != 1:
            raise ApiError(
                409,
                409,
                "目标目录已有内容，但运行时版本较旧、无法确认是否与当前目录共享，为避免误覆盖"
                "已停止换机；请先升级两端运行时，或改用「使用已有目录」",
            )
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


async def require_new_assistant_directory(
    relay: RelayServer, bot: Bot, cipher: Cipher | None
) -> None:
    """助理只在开通时初始化：目录已在（里面可能有助理在沙箱里写的链接、Git 配置）就拒绝，
    因为初始化由守护进程在沙箱外读写这个目录。拒绝时不改工作目录状态，就绪的助理照常干活。"""
    try:
        info = await call_agent(
            relay, cipher, "workspace-info", {"working_dir": bot.working_dir, "bot_id": str(bot.id)}
        )
    except (AgentError, TimeoutError) as exc:
        raise ApiError(502, 502, "工作目录初始化失败，请检查运行时后重试") from exc
    if info.get("exists") is not False:
        raise ApiError(422, 422, unsupported("在已有工作目录上重新初始化"))


async def initialize_workspace(session: AsyncSession, bot: Bot, cipher: Cipher | None) -> None:
    if not bot.relay_server_id:
        bot.workspace_state, bot.workspace_phase = "pending", "等待分配运行时"
        return
    relay = await session.get(RelayServer, bot.relay_server_id)
    if relay is None:
        raise ApiError(422, 422, "运行时不存在")
    await memory_transfer.ensure_idle(session, bot)
    if is_assistant(bot):
        if inactive_assistant(bot):
            raise ApiError(409, 409, "个人助理还没开通好或已停用，不能初始化工作目录")
        await require_new_assistant_directory(relay, bot, cipher)
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


def direct_route(source: dict[str, Any], target: dict[str, Any]) -> dict[str, Any] | None:
    """两台节点都在 config.json 配了同一个网络区（如同一间办公室的局域网）且都支持整包传：
    返回源节点的直连信息，压缩包由目标节点直接拉取；否则 None，照旧经平台中转。"""
    here, there = source.get("workspace_direct"), target.get("workspace_direct")
    if not (isinstance(here, dict) and isinstance(there, dict)):
        return None
    if not (source.get("workspace_pack") and target.get("workspace_pack")):
        return None
    zone = here.get("zone")
    if not zone or zone != there.get("zone") or not here.get("address"):
        return None
    if not isinstance(here.get("fingerprint"), str):
        return None
    return here


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
    if not source_capability.get("workspace_read_raw"):
        chunk_size = min(chunk_size, LEGACY_READ_CHUNK)
    route = direct_route(source_capability, capability)
    if route is not None:
        # 直连时两端按放宽后的上限导出、校验清单与压缩包。
        src = {**src, "limits": "direct"}
        dst = {**dst, "limits": "direct"}
    log.info(
        "workspace_transfer_route",
        bot_id=str(bot.id),
        route="direct" if route else "relay",
        zone=route.get("zone") if route else None,
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
    if not isinstance(manifest, list) or len(manifest) > (DIRECT_MAX_FILES if route else MAX_FILES):
        raise ApiError(502, 502, "源文件清单无效")
    if not capability.get("workspace_replace") and (
        occupied or any(".git" in PurePosixPath(entry["path"]).parts for entry in manifest)
    ):
        raise ApiError(409, 409, "目标运行时版本较旧，不能覆盖已有目录或复制 .git，请先升级运行时")
    await call_agent(target, cipher, "workspace-import", {**dst, "manifest": manifest})
    if source_capability.get("workspace_pack") and capability.get("workspace_pack"):
        await transfer_pack(source, target, cipher, src, dst, manifest, chunk_size, route=route)
    else:
        await transfer_each_file(source, target, cipher, src, dst, manifest, chunk_size)
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


async def transfer_pack(
    source: RelayServer,
    target: RelayServer,
    cipher: Cipher | None,
    src: dict[str, Any],
    dst: dict[str, Any],
    manifest: list[dict[str, Any]],
    chunk_size: int,
    *,
    route: dict[str, Any] | None = None,
) -> None:
    """两端都支持时整包传：源节点把清单里的文件打成一个压缩包，按块读走、写到目标，目标核对
    大小与哈希后只按清单解开。调用次数从「每个文件至少一读一写」降到「每块一读一写」。

    `route` 是同一网络区里源节点的直连信息：压缩包由目标节点直接从源节点拉取，平台只下发令牌
    与核对结果。拉不通且目录在中转上限之内时退回按块中转，超出上限就停下（绑定不变）。"""
    if not any("link" not in entry for entry in manifest):
        return
    job = {**src, "transfer_id": dst["transfer_id"]}
    token = secrets.token_hex(32) if route else None
    try:
        create: dict[str, Any] = {**job, "action": "create", "manifest": manifest}
        if token:
            create["serve_token"] = token
        packed = await call_agent(source, cipher, "workspace-pack", create)
        size, pack_hash = packed.get("size"), packed.get("hash")
        if not isinstance(size, int) or size <= 0 or not isinstance(pack_hash, str):
            raise ApiError(502, 502, "源文件压缩包无效")
        if route is not None and token is not None:
            if await direct_fetch(source, target, cipher, dst, route, token, packed, manifest):
                await call_agent(
                    target,
                    cipher,
                    "workspace-unpack",
                    {**dst, "action": "extract", "size": size, "hash": pack_hash},
                )
                return

        async def read(_: dict[str, Any], offset: int) -> dict[str, Any]:
            return await call_agent(
                source,
                cipher,
                "workspace-pack",
                {**job, "action": "read", "offset": offset, "length": chunk_size},
            )

        async def write(_: dict[str, Any], offset: int, chunk: dict[str, Any]) -> None:
            raw = base64.b64decode(chunk["data"], validate=True)
            if len(raw) != min(chunk_size, size - offset):
                raise ApiError(502, 502, "文件传输校验失败")
            await call_agent(
                target,
                cipher,
                "workspace-unpack",
                {**dst, "action": "write", "offset": offset, "data": chunk["data"]},
            )

        steps: list[tuple[dict[str, Any], int]] = [
            ({}, offset) for offset in range(0, size, chunk_size)
        ]
        await transfer_chunks(steps, read, write)
        await call_agent(
            target,
            cipher,
            "workspace-unpack",
            {**dst, "action": "extract", "size": size, "hash": pack_hash},
        )
    finally:
        # 源节点的暂存压缩包用完就删；删不掉也会被过期清理扫掉，不影响结果。
        with contextlib.suppress(AgentError, TimeoutError):
            await call_agent(source, cipher, "workspace-pack", {**job, "action": "remove"})


def within_relay_limits(manifest: list[dict[str, Any]]) -> bool:
    total = sum(int(entry.get("size") or 0) for entry in manifest if "link" not in entry)
    return len(manifest) <= MAX_FILES and total <= MAX_TOTAL


async def direct_fetch(
    source: RelayServer,
    target: RelayServer,
    cipher: Cipher | None,
    dst: dict[str, Any],
    route: dict[str, Any],
    token: str,
    packed: dict[str, Any],
    manifest: list[dict[str, Any]],
) -> bool:
    """让目标节点直接从源节点拉压缩包；拉齐返回 True。拉不通时目标节点已删掉不完整的文件，
    目录在中转上限之内返回 False（调用方接着按块中转），否则抛出。"""
    problem: str | None = "源节点没能开启局域网下载口"
    if packed.get("served"):
        problem = await run_direct_fetch(target, cipher, dst, route, token, int(packed["size"]))
        if problem is None:
            return True
    log.warning(
        "workspace_direct_failed",
        source=str(source.id),
        target=str(target.id),
        zone=route.get("zone"),
        detail=problem,
    )
    if within_relay_limits(manifest):
        return False
    raise ApiError(
        502,
        502,
        f"局域网直连传输失败（{problem}），工作目录又超过经平台中转的上限（5 万个文件或 4G），"
        "原绑定已保留；请确认两台节点在同一网络、源节点的直连端口已放行后重试",
    )


async def run_direct_fetch(
    target: RelayServer,
    cipher: Cipher | None,
    dst: dict[str, Any],
    route: dict[str, Any],
    token: str,
    size: int,
) -> str | None:
    """启动目标节点的后台拉取并等它结束；成功返回 None，失败返回原因。卡住不动时直接抛出：
    后台还在写同一个文件，不能接着按块中转。"""
    try:
        await call_agent(
            target,
            cipher,
            "workspace-unpack",
            {
                **dst,
                "action": "fetch",
                "address": route["address"],
                "fingerprint": route["fingerprint"],
                "token": token,
                "size": size,
            },
        )
    except AgentError as exc:
        return exc.detail or "目标节点没能开始拉取"
    received: object = -1
    moved_at = asyncio.get_running_loop().time()
    while True:
        await asyncio.sleep(DIRECT_POLL_SECONDS)
        state = await call_agent(
            target, cipher, "workspace-unpack", {**dst, "action": "fetch-status"}
        )
        if state.get("done"):
            return None
        if state.get("error"):
            return str(state["error"])[:200]
        now = asyncio.get_running_loop().time()
        if state.get("received") != received:
            received, moved_at = state.get("received"), now
        elif now - moved_at > DIRECT_STALL_SECONDS:
            raise ApiError(
                502,
                502,
                "局域网直连传输卡住超过 2 分钟，原绑定已保留；请确认两台节点之间的网络后重试",
            )


async def transfer_each_file(
    source: RelayServer,
    target: RelayServer,
    cipher: Cipher | None,
    src: dict[str, Any],
    dst: dict[str, Any],
    manifest: list[dict[str, Any]],
    chunk_size: int,
) -> None:
    """旧运行时：逐个文件按块读、写。"""
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
                "transfer_id": dst["transfer_id"],
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


async def copy_sessions(
    session: AsyncSession,
    bot: Bot,
    source: RelayServer,
    target: RelayServer,
    source_dir: str,
    target_dir: str,
    cipher: Cipher | None,
    progress: Callable[[int, int], Awaitable[None]] | None = None,
) -> set[uuid.UUID]:
    """把会话文件搬到目标运行时，返回搬成功的 relay 会话；搬不了的由调用方改为文本续接。

    只做同后端：Claude → Claude 搬对话记录，Codex → Codex 搬 thread 的 rollout 与绑定（Codex
    凭 rollout 自行补上本机状态库），OpenCode → OpenCode 搬导出的会话与绑定（目标机驱动导入）。
    两端都要报对应能力位，老节点不报就全部文本续接。
    尽力而为，失败不影响换机。`progress(已完成, 总数)` 在每个会话搬完后调用。
    """
    provider = source.model_provider
    if provider not in SESSION_CAPABILITY or target.model_provider != provider:
        return set()
    rows = [row for row in await sessions.list_for_bot(session, bot.id) if row.backend == provider]
    if not rows:
        return set()
    copied: set[uuid.UUID] = set()
    transfer_id = str(uuid.uuid4())
    try:
        async with asyncio.timeout(SESSION_COPY_TIMEOUT):
            for relay in (source, target):
                ping = await call_agent(relay, cipher, "ping")
                if ping.get(SESSION_CAPABILITY[provider]) != 1:
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
            if listing.get("skipped"):
                # 超过单会话上限的不搬（如几百 MB 的长会话），它们首轮改为文本续接。
                log.warning(
                    "session_copy_oversized",
                    bot_id=str(bot.id),
                    sessions=[item.get("session") for item in listing["skipped"]],
                )
            grouped: dict[str, list[dict[str, Any]]] = {}
            for item in listing.get("files") or []:
                # 老节点不报 session，Claude 的路径以会话 id 开头。
                sid = item.get("session") or str(item["path"]).split("/", 1)[0].removesuffix(
                    ".jsonl"
                )
                grouped.setdefault(sid, []).append(item)
            limit = asyncio.Semaphore(SESSION_COPY_CONCURRENCY)

            async def one(sid: str, items: list[dict[str, Any]]) -> str | None:
                async with limit:
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
                        return None
                    return sid

            # 进度回调会提交数据库会话，只在这一个协程里调，不进并行的搬运协程。
            jobs = [asyncio.ensure_future(one(sid, items)) for sid, items in grouped.items()]
            try:
                for done, finished in enumerate(asyncio.as_completed(jobs), 1):
                    sid = await finished
                    if sid is not None:
                        copied.add(uuid.UUID(sid))
                    if progress is not None:
                        await progress(done, len(jobs))
            finally:
                for job in jobs:
                    job.cancel()
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


async def prepare_assistant_directory(
    session: AsyncSession,
    bot: Bot,
    target: RelayServer,
    cipher: Cipher | None,
    *,
    directory: str,
    operation_id: uuid.UUID,
    reused: bool,
    exists: bool,
) -> tuple[str, bool, set[uuid.UUID]]:
    """助理换运行时：原目录里的东西一样不带（不导出、不克隆、不迁会话与记忆），这些都会由守护进程
    在沙箱外读写助理写过的目录。还是原来那个目录（同一节点同一路径，或共享盘上的同一份）就原样留着、
    节点上什么都不做；否则目标目录必须还不存在，建一个全新的空目录。抛出的错误由调用方走迁移失败的
    收尾：绑定不变、回到 ready、清掉迁移标记。"""
    if not reused:
        if exists:
            raise ApiError(422, 422, unsupported("换到已有内容的工作目录"))
        bot.workspace_phase = "初始化全新的工作目录"
        await session.commit()
        await call_agent(
            target,
            cipher,
            "workspace-init",
            {"working_dir": directory, "bot_id": str(bot.id), "content": instruction_content(bot)},
        )
    await session.refresh(bot, with_for_update=True)
    if bot.workspace_operation_id != operation_id:
        raise ApiError(409, 409, "迁移状态已变化，请刷新后重试")
    if inactive_assistant(bot):
        # 建目录期间放开了行锁，停用（或别的流程）趁这时开始了：不换过去。刚建的目录没人会用，
        # 登记待删；原绑定与原目录照旧，由停用流程收拾。
        if not reused:
            from coreman.core.assistants import retired_dirs

            await retired_dirs.retire(
                session, bot_id=bot.id, relay_id=target.id, working_dir=directory
            )
        raise ApiError(409, 409, ASSISTANT_NOT_ACTIVE_MESSAGE)
    await reserve_workspace(session, target.id, directory, bot_id=bot.id)
    bot.workspace_phase = "完成切换"
    return "not_transferred", reused, set()


async def prepare_switch(
    session: AsyncSession,
    bot: Bot,
    target: RelayServer,
    cipher: Cipher | None,
    *,
    mode: WorkspaceMode = "copy",
    directory: str,
    allow_stored_memory: bool = False,
    drain: bool = False,
    cancelled: asyncio.Event | None = None,
) -> tuple[str, bool, set[uuid.UUID]]:
    """Caller owns bot row lock. Intermediate commits persist fence and recovered memory.

    返回记忆状态、目标是不是原来那个目录（同一节点同一目录，或共享盘上的同一份：是的话文件
    一个没动，技能也都还在），以及会话文件已搬到目标运行时的 relay 会话。
    每一步都写进 workspace_phase 并提交，管理台据此显示进度。

    Final binding and ready state remain caller-transactional. A process crash leaves a
    durable fence; retry is allowed after the 30 minute deadline (work has a 25m timeout).

    `drain=True`（后台切换作业）：不要求员工空闲，先隔离（不再认领新任务），再等已在跑的
    这一轮结束；`cancelled` 置位时放弃。同步调用仍要求空闲，有任务在跑直接 409。
    """
    if bot.workspace_state in {"migrating", "initializing", "busy"}:
        if bot.workspace_deadline is None or bot.workspace_deadline > utcnow():
            raise ApiError(409, 409, "工作目录操作正在进行，请等待完成")
    if not drain:
        await memory_transfer.ensure_idle(session, bot)
    old = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
    same_directory = bool(
        old and old.runtime_node_id == target.runtime_node_id and directory == bot.working_dir
    )
    operation_id = uuid.uuid4()
    bot.workspace_state, bot.workspace_error = "migrating", None
    bot.workspace_phase = "等待进行中的任务结束" if drain else "检查目标目录"
    bot.workspace_operation_id = operation_id
    bot.workspace_target_relay_id, bot.workspace_target_dir = target.id, directory
    bot.workspace_deadline = utcnow() + timedelta(minutes=30)
    await session.commit()
    try:
        if drain:
            await memory_transfer.drain(session, bot, cancelled=cancelled)
            bot.workspace_phase = "检查目标目录"
            bot.workspace_deadline = utcnow() + timedelta(minutes=30)
            await session.commit()
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
            # 目标目录可能就是共享盘上的同一份（路径可以不同，归属标记也可能被 git clean -x 删掉），
            # 先探测：是就不复制、不重装技能，两种处理方式结果一样。限流卡片的自动切换默认就是复制。
            shared = False
            if not same_directory and old and occupied:
                bot.workspace_phase = "确认是否为共享存储"
                await session.commit()
                try:
                    shared = await shared_directory(bot, old, target, directory, cipher)
                except AgentError as exc:
                    raise ApiError(
                        502,
                        502,
                        "无法确认目标目录是否与当前目录共享，为避免误覆盖已停止换机；"
                        "请确认两端运行时在线后重试",
                    ) from exc
            if is_assistant(bot):
                return await prepare_assistant_directory(
                    session,
                    bot,
                    target,
                    cipher,
                    directory=directory,
                    operation_id=operation_id,
                    reused=same_directory or shared,
                    exists=info.get("exists") is not False,
                )
            bot.workspace_phase = "回收最新记忆"
            await session.commit()
            status = await memory_transfer.recover(
                session,
                bot,
                cipher,
                allow_stored=allow_stored_memory and mode != "copy",
            )
            await memory_transfer.require_shared_layout(session, bot, target, capability)
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
            carried: set[uuid.UUID] = set()
            if old is not None:
                bot.workspace_phase = "迁移会话记录"
                await session.commit()

                async def report(done: int, total: int) -> None:
                    bot.workspace_phase = f"迁移会话记录（{done}/{total}）"
                    await session.commit()

                carried = await copy_sessions(
                    session, bot, old, target, bot.working_dir, directory, cipher, report
                )
            # Reacquire lock before final binding, fail closed on stale operation identity.
            await session.refresh(bot, with_for_update=True)
            if bot.workspace_operation_id != operation_id:
                raise ApiError(409, 409, "迁移状态已变化，请刷新后重试")
            await reserve_workspace(session, target.id, directory, bot_id=bot.id)
            bot.workspace_phase = "完成切换"
            return status, same_directory or shared, carried
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
        # 绑定和原目录都没动过，员工继续在原运行时上干活：回到 ready（任务认领只认它），
        # 原因留在 workspace_error / workspace_phase 给工作目录面板看，重试仍走切换运行时。
        # 目标预留一并释放，不然目标运行时会一直显示「有员工正在迁移过来」。
        bot.workspace_state, bot.workspace_phase = "ready", "迁移失败，原绑定已保留"
        bot.workspace_error = failure_message(exc)
        bot.workspace_deadline, bot.workspace_operation_id = None, None
        bot.workspace_target_relay_id, bot.workspace_target_dir = None, None
        await session.commit()
        if isinstance(exc, ApiError):
            raise
        raise ApiError(502, 502, bot.workspace_error) from exc


async def release_fence(session: AsyncSession, bot_id: uuid.UUID, reason: str) -> bool:
    """切换作业中途没了（worker 被杀、发布重启、未预期的异常）时解除迁移隔离。

    绑定只在作业最后一个事务里改，中途断掉时员工仍在原运行时上：回到 ready 让任务照常认领，
    原因留给工作目录面板。不在迁移中的不动。返回是否解除了。
    """
    bot = await session.get(Bot, bot_id, with_for_update=True, populate_existing=True)
    if bot is None or bot.workspace_state != "migrating":
        return False
    bot.workspace_state, bot.workspace_phase = "ready", "切换中断，原绑定已保留"
    bot.workspace_error = reason
    bot.workspace_deadline, bot.workspace_operation_id = None, None
    bot.workspace_target_relay_id, bot.workspace_target_dir = None, None
    return True

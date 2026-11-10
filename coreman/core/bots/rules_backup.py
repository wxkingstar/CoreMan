"""员工规则文档每日备份：AGENTS.md、CLAUDE.md 与 docs/ 下的文本文件提交到一个共用的 GitHub 仓库。

每个员工一个目录 `bots/<bot_key>/`，根目录的 `INDEX.md` 列出标识与名称；一轮只产生一个提交，内容没变
就不提交。仓库令牌只在平台进程里：节点只经已有的工作区只读操作（Git 状态、列目录、读文件）交出内容，
不需要升级节点，节点也碰不到 GitHub。

不备份的员工：根目录自己就是带远端的 Git 仓库，或节点拒绝在其中执行 git 的仓库（规则随那个仓库
维护），以及停用、未分配运行时、工作目录不在就绪状态的员工。读取失败（节点离线等）的员工保留仓库里上一次的内容，下一轮再试；
已删除员工的目录也原样保留。提交前把常见格式的令牌、私钥与 `password: ...` 一类赋值
替换成 [REDACTED]。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.db.models import Bot, RelayServer
from coreman.core.logging import get_logger
from coreman.core.relay.agent_client import AgentError, call_agent
from coreman.core.settings_store import SettingsStore

log = get_logger(__name__)

RULE_FILES = ("AGENTS.md", "CLAUDE.md")
DOCS_DIR = "docs"
TEXT_SUFFIXES = frozenset(
    {".md", ".markdown", ".mdx", ".txt", ".rst", ".yaml", ".yml", ".json", ".toml"}
)
MAX_FILE_BYTES = 512 * 1024
MAX_BOT_BYTES = 8 * 1024 * 1024
MAX_BOT_FILES = 500
MAX_DEPTH = 8
BOT_TIMEOUT_SECONDS = 300
INTERVAL = timedelta(hours=24)
# 整轮失败（GitHub 不可达、令牌失效）时隔一小时再试，不等一整天。
RETRY = timedelta(hours=1)
STATE_KEY = "rules_backup.last_run"
ROOT = "bots"
INDEX = "INDEX.md"
REDACTED = "[REDACTED]"
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

SECRET_TOKENS = re.compile(
    r"\bgh[pousr]_[A-Za-z0-9]{30,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{40,}"
    r"|\bglpat-[A-Za-z0-9_-]{20,}"
    r"|\bsk-[A-Za-z0-9_-]{20,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bLTAI[A-Za-z0-9]{12,}"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
)
SECRET_ASSIGNMENTS = re.compile(
    r"(?i)(\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)[\w-]*"
    r"[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9_\-./+=]{12,})"
)


class BackupError(Exception):
    """str() 是可以写进日志和状态的固定文案，不带令牌与文件内容。"""


def redact(text: str) -> tuple[str, int]:
    """把疑似密钥替换成 [REDACTED]，返回替换后的文本与替换处数。"""
    text, tokens = SECRET_TOKENS.subn(REDACTED, text)
    text, assignments = SECRET_ASSIGNMENTS.subn(lambda m: m.group(1) + REDACTED, text)
    return text, tokens + assignments


def blob_sha(text: str) -> str:
    """与 Git 一致的 blob 哈希：和仓库里的现有文件比对，内容没变就不进提交。"""
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


@dataclass
class Collected:
    files: dict[str, str] = field(default_factory=dict)
    # 超过大小、数量上限或不是 UTF-8 文本而没有备份的文件
    skipped: list[str] = field(default_factory=list)
    redacted: int = 0


async def collect(relay: RelayServer, bot: Bot) -> Collected | None:
    """从员工所在节点读出规则文档；根目录自己是带远端的 Git 仓库时返回 None。"""
    base = {"working_dir": bot.working_dir, "bot_id": str(bot.id)}

    async def call(operation: str, **payload: Any) -> dict[str, Any]:
        return await call_agent(relay, None, operation, {**payload, **base})

    if (await call_agent(relay, None, "ping")).get("workspace_protocol") != 1:
        raise BackupError("运行时版本过旧，不支持读取工作目录")
    try:
        status = await call("workspace-git-status")
    except AgentError as exc:
        # 节点只在根目录确有 .git 时才执行 git；拒绝执行（仓库配置了凭证助手、url 改写等）
        # 或执行失败，说明它自己是个仓库，规则随那个仓库维护。
        if exc.detail and "Git" in exc.detail:
            return None
        raise
    if status.get("initialized") and status.get("remote"):
        return None
    out, total = Collected(), 0

    async def read(path: str, size: int) -> None:
        nonlocal total
        if size > MAX_FILE_BYTES or total + size > MAX_BOT_BYTES or len(out.files) >= MAX_BOT_FILES:
            out.skipped.append(path)
            return
        data = await call("workspace-read", path=path, offset=0, length=max(size, 1))
        content = data.get("content")
        if not isinstance(content, str) or not data.get("eof"):
            out.skipped.append(path)
            return
        text, hits = redact(content)
        out.files[path], out.redacted, total = text, out.redacted + hits, total + size

    entries = (await call("workspace-list", path="")).get("entries", [])
    # AGENTS.md 与 CLAUDE.md 是一份正本加一个链接：链接不备份，只备份正本（两份都是文件就都备份）。
    for entry in entries:
        if entry.get("name") in RULE_FILES and entry.get("type") == "file":
            await read(entry["path"], int(entry.get("size") or 0))
    if not any(e.get("name") == DOCS_DIR and e.get("type") == "directory" for e in entries):
        return out
    queue = [(DOCS_DIR, 1)]
    while queue:
        directory, depth = queue.pop(0)
        listing = await call("workspace-list", path=directory)
        if listing.get("truncated"):
            out.skipped.append(f"{directory}/")
        for entry in listing.get("entries", []):
            path, kind = entry.get("path", ""), entry.get("type")
            if kind == "directory":
                if depth < MAX_DEPTH:
                    queue.append((path, depth + 1))
                else:
                    out.skipped.append(f"{path}/")
            elif kind == "file":
                if PurePosixPath(path).suffix.lower() in TEXT_SUFFIXES:
                    await read(path, int(entry.get("size") or 0))
                else:
                    out.skipped.append(path)
    return out


class GitHub:
    """经 Git Data API 在默认分支上追加一个提交；不克隆仓库，只列一次目录树。"""

    def __init__(self, repo: str, token: str, http: httpx.AsyncClient) -> None:
        self.repo, self.token, self.http = repo, token, http

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self.http.request(
                method,
                f"https://api.github.com/repos/{self.repo}{path}",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=60,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise BackupError(f"连接 GitHub 失败（{type(exc).__name__}）") from exc
        if response.status_code == 409 and path.startswith("/git/ref/"):
            raise BackupError("备份仓库还没有任何提交，请先在仓库里创建一个初始提交")
        if response.status_code in (401, 403, 404):
            raise BackupError(
                f"GitHub 拒绝访问（HTTP {response.status_code}），"
                "请检查仓库名与令牌的 Contents 写权限"
            )
        if response.status_code >= 400:
            raise BackupError(f"GitHub 请求失败（HTTP {response.status_code}）")
        return response.json()

    async def head(self) -> tuple[str, str, str]:
        """默认分支名、它的最新提交与目录树。"""
        branch = (await self.request("GET", ""))["default_branch"]
        commit = (await self.request("GET", f"/git/ref/heads/{branch}"))["object"]["sha"]
        tree = (await self.request("GET", f"/git/commits/{commit}"))["tree"]["sha"]
        return branch, commit, tree

    async def blobs(self, tree: str) -> dict[str, str]:
        data = await self.request("GET", f"/git/trees/{tree}", params={"recursive": "1"})
        if data.get("truncated"):
            raise BackupError("备份仓库文件过多，GitHub 无法列出完整目录树")
        return {e["path"]: e["sha"] for e in data.get("tree", []) if e.get("type") == "blob"}

    async def commit(
        self, branch: str, parent: str, base_tree: str, entries: list[dict[str, Any]], message: str
    ) -> str:
        tree = await self.request(
            "POST", "/git/trees", json={"base_tree": base_tree, "tree": entries}
        )
        commit = await self.request(
            "POST",
            "/git/commits",
            json={"message": message, "tree": tree["sha"], "parents": [parent]},
        )
        # 不强推：别人同时往这个分支推过，这一轮就失败，一小时后基于新提交重来。
        await self.request(
            "PATCH", f"/git/refs/heads/{branch}", json={"sha": commit["sha"], "force": False}
        )
        return str(commit["sha"])


def upsert(path: str, text: str) -> dict[str, Any]:
    return {"path": path, "mode": "100644", "type": "blob", "content": text}


def plan(existing: dict[str, str], desired: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    """只改 desired 里各员工目录下的文件：新增、修改，以及员工那边已经删掉的文件。"""
    entries: list[dict[str, Any]] = []
    for key, files in sorted(desired.items()):
        prefix = f"{ROOT}/{key}/"
        wanted = {prefix + path: text for path, text in files.items()}
        for path, text in sorted(wanted.items()):
            if existing.get(path) != blob_sha(text):
                entries.append(upsert(path, text))
        for path in sorted(existing):
            if path.startswith(prefix) and path not in wanted:
                entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
    return entries


def index(keys: set[str], names: dict[str, str]) -> str:
    rows = [
        f"| `{key}` | {names.get(key, '（已删除）').replace('|', '/')} |" for key in sorted(keys)
    ]
    return "\n".join(
        [
            "# AI 员工规则文档备份",
            "",
            "CoreMan 每天自动提交，请勿手动修改。每个员工一个目录：`bots/<标识>/`。",
            "",
            "| 标识 | 名称 |",
            "| --- | --- |",
            *rows,
            "",
        ]
    )


async def run(
    factory: async_sessionmaker[AsyncSession],
    repo: str,
    token: str,
    now: datetime,
    *,
    apply: bool = True,
    http: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """备份一轮，返回可以写进状态与日志的摘要（不含文件内容）。apply=False 只读不提交。"""
    if not REPO_RE.fullmatch(repo):
        raise BackupError("RULES_BACKUP_REPO 必须是 owner/name 格式")
    async with factory() as session:
        rows = (
            await session.execute(
                select(Bot, RelayServer)
                .outerjoin(RelayServer, RelayServer.id == Bot.relay_server_id)
                .order_by(Bot.bot_key)
            )
        ).all()
        session.expunge_all()
    names = {bot.bot_key: bot.name for bot, _ in rows}
    bots: dict[str, dict[str, Any]] = {}
    desired: dict[str, dict[str, str]] = {}
    inactive = 0
    for bot, relay in rows:
        if not (
            bot.enabled
            and bot.working_dir
            and bot.workspace_state == "ready"
            and relay is not None
            and relay.is_active
            and relay.runtime_node_id is not None
        ):
            inactive += 1
            continue
        try:
            async with asyncio.timeout(BOT_TIMEOUT_SECONDS):
                collected = await collect(relay, bot)
        except (AgentError, BackupError, TimeoutError) as exc:
            reason = str(exc) or "读取超时"
            if isinstance(exc, AgentError) and exc.detail:
                reason = f"{reason}：{exc.detail}"
            bots[bot.bot_key] = {"status": "failed", "reason": reason}
            log.warning("rules_backup_bot_failed", bot_key=bot.bot_key, reason=reason)
            continue
        if collected is None:
            bots[bot.bot_key] = {"status": "own_repo"}
            continue
        desired[bot.bot_key] = collected.files
        bots[bot.bot_key] = {
            "status": "unchanged",
            "files": len(collected.files),
            "skipped": collected.skipped[:20],
            "redacted": collected.redacted,
        }
    own_http = http is None
    client = http or httpx.AsyncClient()
    try:
        github = GitHub(repo, token, client)
        branch, parent, base_tree = await github.head()
        existing = await github.blobs(base_tree)
        entries = plan(existing, desired)
        final = set(existing)
        for entry in entries:
            if "content" in entry:
                final.add(entry["path"])
            else:
                final.discard(entry["path"])
        keys = {p.split("/")[1] for p in final if p.startswith(f"{ROOT}/") and p.count("/") > 1}
        text = index(keys, names)
        if existing.get(INDEX) != blob_sha(text):
            entries.append(upsert(INDEX, text))
        for entry in entries:
            parts = entry["path"].split("/")
            if parts[0] == ROOT and parts[1] in bots:
                bots[parts[1]]["status"] = "updated"
                bots[parts[1]]["changed"] = bots[parts[1]].get("changed", 0) + 1
        commit = None
        if entries and apply:
            updated = sum(1 for b in bots.values() if b["status"] == "updated")
            message = f"备份 {updated} 个 AI 员工的规则文档（{now.date().isoformat()}）"
            commit = await github.commit(branch, parent, base_tree, entries, message)
    finally:
        if own_http:
            await client.aclose()
    return {
        "at": now.isoformat(),
        "repo": repo,
        "commit": commit,
        "changed_files": len(entries),
        "inactive": inactive,
        "bots": bots,
    }


async def run_due(
    factory: async_sessionmaker[AsyncSession],
    store: SettingsStore,
    repo: str,
    token: str,
    now: datetime,
) -> dict[str, Any] | None:
    """距上一轮满一天（上一轮整体失败则满一小时）就备份一轮并记下结果；没到期返回 None。"""
    last = await store.get(STATE_KEY) or {}
    if last.get("at"):
        wait = RETRY if last.get("error") else INTERVAL
        if now - datetime.fromisoformat(last["at"]) < wait:
            return None
    try:
        report = await run(factory, repo, token, now)
    except BackupError as exc:
        report = {"at": now.isoformat(), "repo": repo, "error": str(exc)}
    await store.set(STATE_KEY, report)
    return report

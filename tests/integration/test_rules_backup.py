"""员工规则文档每日备份：从节点读 AGENTS.md / docs/，经 Git Data API 一次提交到共用仓库。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from coreman.core.bots import rules_backup
from coreman.core.db.models import Bot, User
from coreman.core.db.session import make_session_factory
from coreman.core.relay.agent_client import AgentError
from coreman.core.settings_store import SettingsStore
from tests.fakes.runtime_node import add_node_relay

NOW = datetime(2026, 11, 10, 3, tzinfo=UTC)
TOKEN = "ghp_" + "a" * 36


def node_files(**files: str | None) -> dict[str, Any]:
    """路径 → 文本；值为 None 表示链接。目录由路径推出。"""
    return {"git": {"initialized": False}, "files": files}


NODES: dict[str, dict[str, Any]] = {
    "/w/alpha": node_files(
        **{
            "AGENTS.md": "# Alpha\n\nSee docs/refunds.md\n",
            "CLAUDE.md": None,
            "docs/refunds.md": f"退款规则\napi_key: {'k' * 20}\n用 {TOKEN} 调用\n",
            "docs/sub/limits.yaml": "max: 3\n",
            "docs/logo.png": "binary",
            "docs/huge.md": "x" * (rules_backup.MAX_FILE_BYTES + 1),
            "notes.md": "不在备份范围",
        }
    ),
    "/w/repo": {"git": {"initialized": True, "remote": "https://example.com/r.git"}, "files": {}},
}


def entries_of(files: dict[str, str | None], directory: str) -> list[dict[str, Any]]:
    prefix = f"{directory}/" if directory else ""
    seen: dict[str, dict[str, Any]] = {}
    for path, text in files.items():
        if not path.startswith(prefix):
            continue
        head, _, rest = path[len(prefix) :].partition("/")
        full = prefix + head
        if rest:
            seen[head] = {"name": head, "path": full, "type": "directory", "size": 0}
        else:
            kind = "link" if text is None else "file"
            size = 0 if text is None else len(text.encode())
            seen[head] = {"name": head, "path": full, "type": kind, "size": size}
    return list(seen.values())


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []

    async def call_agent(relay, cipher, operation, payload=None):
        payload = payload or {}
        calls.append((operation, payload))
        if operation == "ping":
            return {"success": True, "workspace_protocol": 1}
        node = NODES.get(payload["working_dir"])
        if node is None:
            raise AgentError("Agent 连接失败（ConnectError）")
        if operation == "workspace-git-status":
            return {"success": True, **node["git"]}
        if operation == "workspace-list":
            return {"success": True, "entries": entries_of(node["files"], payload["path"])}
        if operation == "workspace-read":
            text = node["files"][payload["path"]]
            assert payload["length"] >= len(text.encode())
            binary = payload["path"].endswith(".png")
            return {"success": True, "content": None if binary else text, "eof": True}
        raise AssertionError(operation)

    monkeypatch.setattr(rules_backup, "call_agent", call_agent)
    return calls


class FakeGitHub:
    def __init__(self, tree: dict[str, str], *, empty: bool = False) -> None:
        self.tree, self.empty = tree, empty
        self.commits: list[dict[str, Any]] = []
        self.trees: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret-token"
        path = request.url.path.removeprefix("/repos/acme/rules")
        if request.method == "GET" and path == "":
            return httpx.Response(200, json={"default_branch": "main"})
        if path == "/git/ref/heads/main":
            if self.empty:
                return httpx.Response(409, json={})
            return httpx.Response(200, json={"object": {"sha": "c0"}})
        if path == "/git/commits/c0":
            return httpx.Response(200, json={"tree": {"sha": "t0"}})
        if path == "/git/trees/t0":
            tree = [{"path": p, "type": "blob", "sha": s} for p, s in self.tree.items()]
            return httpx.Response(200, json={"tree": tree, "truncated": False})
        if request.method == "POST" and path == "/git/trees":
            self.trees.append(json.loads(request.content))
            return httpx.Response(201, json={"sha": "t1"})
        if request.method == "POST" and path == "/git/commits":
            self.commits.append(json.loads(request.content))
            return httpx.Response(201, json={"sha": "c1"})
        if request.method == "PATCH" and path == "/git/refs/heads/main":
            assert json.loads(request.content) == {"sha": "c1", "force": False}
            return httpx.Response(200, json={})
        raise AssertionError(f"{request.method} {path}")

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


async def seed(engine: AsyncEngine) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        user = User(login_name="owner", display_name="Owner", role="member", source="sync")
        session.add(user)
        await session.flush()
        relay = await add_node_relay(session)

        def bot(key: str, working_dir: str, **values: Any) -> Bot:
            return Bot(
                bot_key=key,
                name=f"员工 {key}",
                platform="feishu",
                created_by=user.id,
                relay_server_id=relay.id,
                model="claude-sonnet-4-6",
                working_dir=working_dir,
                credentials_enc="unused",
                **values,
            )

        session.add_all(
            [
                bot("alpha", "/w/alpha"),
                bot("repo", "/w/repo", dev_mode=True),
                bot("offline", "/w/offline"),
                bot("disabled", "/w/alpha", enabled=False),
                bot("moving", "/w/alpha", workspace_state="migrating"),
            ]
        )
        await session.commit()


def test_redact_replaces_tokens_and_secret_assignments() -> None:
    text, hits = rules_backup.redact(
        f"token={TOKEN}\nPASSWORD: 'hunter2hunter2'\ntoken: 去飞书后台拿\n"
        "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n"
    )
    assert TOKEN not in text and "hunter2" not in text and "abc" not in text
    assert "token: 去飞书后台拿" in text
    assert hits == 3


def test_plan_only_touches_collected_bots() -> None:
    same = "# A\n"
    existing = {
        "bots/a/AGENTS.md": rules_backup.blob_sha(same),
        "bots/a/docs/old.md": "x",
        "bots/b/AGENTS.md": "y",
    }
    entries = rules_backup.plan(existing, {"a": {"AGENTS.md": same, "docs/new.md": "new"}})
    assert entries == [
        {"path": "bots/a/docs/new.md", "mode": "100644", "type": "blob", "content": "new"},
        {"path": "bots/a/docs/old.md", "mode": "100644", "type": "blob", "sha": None},
    ]


async def test_backup_commits_rule_documents_once(db_engine: AsyncEngine, agent) -> None:
    await seed(db_engine)
    kept = "bots/offline/AGENTS.md"
    github = FakeGitHub({kept: "s1", "bots/alpha/docs/old.md": "s2"})
    async with github.client() as http:
        report = await rules_backup.run(
            make_session_factory(db_engine), "acme/rules", "secret-token", NOW, http=http
        )

    assert report["commit"] == "c1"
    assert report["inactive"] == 2
    assert report["bots"]["repo"] == {"status": "own_repo"}
    assert report["bots"]["offline"]["status"] == "failed"
    alpha = report["bots"]["alpha"]
    assert alpha["status"] == "updated" and alpha["files"] == 3 and alpha["redacted"] == 2
    assert sorted(alpha["skipped"]) == ["docs/huge.md", "docs/logo.png"]

    (tree,) = github.trees
    assert tree["base_tree"] == "t0"
    changes = {e["path"]: e.get("content", e.get("sha")) for e in tree["tree"]}
    assert set(changes) == {
        "bots/alpha/AGENTS.md",
        "bots/alpha/docs/refunds.md",
        "bots/alpha/docs/sub/limits.yaml",
        "bots/alpha/docs/old.md",
        "INDEX.md",
    }
    assert changes["bots/alpha/docs/old.md"] is None
    assert TOKEN not in changes["bots/alpha/docs/refunds.md"]
    assert "k" * 20 not in changes["bots/alpha/docs/refunds.md"]
    assert "`alpha` | 员工 alpha" in changes["INDEX.md"]
    assert "`offline` | 员工 offline" in changes["INDEX.md"]
    assert github.commits[0]["parents"] == ["c0"]
    # 链接（CLAUDE.md → AGENTS.md）与 docs/ 之外的文件不读
    read = [p["path"] for op, p in agent if op == "workspace-read"]
    assert "CLAUDE.md" not in read and "notes.md" not in read


async def test_dry_run_reads_but_does_not_commit(db_engine: AsyncEngine, agent) -> None:
    await seed(db_engine)
    github = FakeGitHub({})
    async with github.client() as http:
        factory = make_session_factory(db_engine)
        first = await rules_backup.run(
            factory, "acme/rules", "secret-token", NOW, apply=False, http=http
        )
    assert first["commit"] is None and first["changed_files"] > 0 and github.trees == []


async def test_run_due_waits_a_day_and_retries_failures_hourly(
    db_engine: AsyncEngine, agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed(db_engine)
    factory = make_session_factory(db_engine)
    store = SettingsStore(factory, ttl_seconds=0)
    empty = FakeGitHub({}, empty=True)
    original = rules_backup.run

    async def run(*args, **kwargs):
        async with empty.client() as http:
            return await original(*args, **kwargs, http=http)

    monkeypatch.setattr(rules_backup, "run", run)
    report = await rules_backup.run_due(factory, store, "acme/rules", "secret-token", NOW)
    assert report is not None and "初始提交" in report["error"]
    later = NOW + timedelta(minutes=30)
    assert await rules_backup.run_due(factory, store, "acme/rules", "t", later) is None
    empty.empty = False
    retried = await rules_backup.run_due(
        factory, store, "acme/rules", "secret-token", NOW + timedelta(hours=1)
    )
    assert retried is not None and retried["commit"] == "c1"
    next_day = NOW + timedelta(hours=24)
    assert await rules_backup.run_due(factory, store, "acme/rules", "t", next_day) is None


async def test_rejects_malformed_repo(db_engine: AsyncEngine) -> None:
    with pytest.raises(rules_backup.BackupError):
        await rules_backup.run(make_session_factory(db_engine), "not a repo", "t", NOW)

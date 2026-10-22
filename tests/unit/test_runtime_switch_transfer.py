"""换机：可克隆的仓库只传描述、目标重新克隆；Claude 会话文件随之搬走。"""

import base64
import hashlib
import subprocess
import uuid

import pytest

import runtime_daemon.agent as agent_module
from runtime_daemon.agent import Agent, OperationError

REMOTE_URL = "https://example.com/team/app.git"


@pytest.fixture
def agent(tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    return Agent(
        root=root,
        home=tmp_path / "home",
        api_url="https://example.com",
        token="synthetic-test-token-123456789",
        relay_id="test",
        sessions_dir=tmp_path / "relay-sessions",
    )


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """本地裸仓库扮演远端：节点用户的 Git 配置把 example.com 地址改写到它。"""
    bare = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    git(tmp_path, "init", "-q", "-b", "main", str(seed))
    (seed / "tracked.txt").write_text("v1\n")
    (seed / "gone.txt").write_text("delete me\n")
    (seed / "big").mkdir()
    (seed / "big/asset.bin").write_bytes(b"\0" * 4096)
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "init")
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    home = tmp_path / "user-home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        f'[url "file://{bare}"]\n\tinsteadOf = {REMOTE_URL}\n[uploadpack]\n\tallowFilter = true\n'
    )
    monkeypatch.setenv("HOME", str(home))
    original = agent_module.run_command

    def run_command(command, *args, **kwargs):
        # 测试的远端是本机路径，只在这里放开 file 协议。
        command = [
            "protocol.file.allow=always" if c == "protocol.file.allow=never" else c for c in command
        ]
        return original(command, *args, **kwargs)

    monkeypatch.setattr(agent_module, "run_command", run_command)
    return bare


def call(agent, kind, working_dir, **kw):
    return agent.dispatch(
        {"type": "workspace-" + kind, "working_dir": working_dir, "bot_id": "b1", **kw}
    )


def migrate(agent, source, target, *, expand=()):
    export = call(agent, "export", source, git=True, expand=list(expand))
    transfer = str(uuid.uuid4())
    cloned = []
    for repo in export["repos"]:
        call(agent, "clone", target, transfer_id=transfer, repo=repo)
        cloned.append(repo)
    manifest = export["manifest"]
    call(agent, "import", target, transfer_id=transfer, manifest=manifest)
    for entry in manifest:
        if "link" not in entry:
            data = call(agent, "read", source, path=entry["path"], transfer_id=transfer)["data"]
            call(agent, "import", target, transfer_id=transfer, path=entry["path"], data=data)
    call(agent, "finish", target, transfer_id=transfer, replace=True, repos=cloned)
    return export


def make_source(agent, remote):
    call(agent, "init", "one")
    repo = agent.root / "one/src/app"
    git(agent.root / "one", "clone", "-q", str(remote), str(repo))
    git(repo, "remote", "set-url", "origin", REMOTE_URL)
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "tracked.txt").write_text("v2 committed locally\n")
    git(repo, "commit", "-q", "-am", "local only")
    (repo / "tracked.txt").write_text("v3 not committed\n")
    (repo / "gone.txt").unlink()
    (repo / "notes.md").write_text("untracked\n")
    (agent.root / "one/report.txt").write_text("outside repo\n")
    return repo


def test_clonable_repo_ships_only_changes_and_restores_branches(agent, remote):
    repo = make_source(agent, remote)
    head = git(repo, "rev-parse", "HEAD")
    export = migrate(agent, "one", "two")
    paths = {entry["path"] for entry in export["manifest"]}
    # 与 HEAD 一致的已跟踪文件和 .git 不复制，改动、未跟踪与仓库外的文件照常复制。
    assert "src/app/big/asset.bin" not in paths
    assert not any(".git" in p.split("/") for p in paths)
    assert {"src/app/tracked.txt", "src/app/notes.md", "report.txt"} <= paths
    (described,) = export["repos"]
    assert described["path"] == "src/app" and described["bundle"]
    assert described["deleted"] == ["gone.txt"]

    target = agent.root / "two/src/app"
    assert git(target, "rev-parse", "HEAD") == head
    assert git(target, "symbolic-ref", "--short", "HEAD") == "feature"
    assert sorted(git(target, "branch", "--format=%(refname:short)").split()) == [
        "feature",
        "main",
    ]
    assert git(target, "rev-parse", "--abbrev-ref", "main@{upstream}") == "origin/main"
    assert (target / "tracked.txt").read_text() == "v3 not committed\n"
    assert (target / "big/asset.bin").read_bytes() == b"\0" * 4096
    assert not (target / "gone.txt").exists()
    assert (target / "notes.md").read_text() == "untracked\n"
    assert (agent.root / "two/report.txt").read_text() == "outside repo\n"
    assert git(target, "status", "--porcelain") == git(repo, "status", "--porcelain")
    assert call(agent, "info", "two")["owned"]


def test_clone_failure_falls_back_to_full_copy(agent, remote):
    repo = make_source(agent, remote)
    export = call(agent, "export", "one", git=True)
    with pytest.raises(OperationError):
        call(
            agent,
            "clone",
            "two",
            transfer_id=str(uuid.uuid4()),
            repo={**export["repos"][0], "url": "https://example.com/team/missing.git"},
        )
    # 克隆失败的仓库按 expand 整仓复制，包括 .git。
    full = call(agent, "export", "one", git=True, expand=["src/app"])
    assert full["repos"] == []
    assert any(e["path"].startswith("src/app/.git/") for e in full["manifest"])
    migrate(agent, "one", "three", expand=["src/app"])
    target = agent.root / "three/src/app"
    assert git(target, "rev-parse", "HEAD") == git(repo, "rev-parse", "HEAD")
    assert (target / "big/asset.bin").exists()


def test_repo_with_credentials_or_stash_is_copied_whole(agent, remote):
    repo = make_source(agent, remote)
    git(repo, "remote", "set-url", "origin", "https://user:secret@example.com/team/app.git")
    assert call(agent, "export", "one", git=True)["repos"] == []
    git(repo, "remote", "set-url", "origin", REMOTE_URL)
    git(repo, "stash", "-q", "-u")
    assert call(agent, "export", "one", git=True)["repos"] == []


def test_legacy_platform_export_is_unchanged(agent, remote):
    make_source(agent, remote)
    export = call(agent, "export", "one")
    assert export["repos"] == []
    assert any(e["path"] == "src/app/big/asset.bin" for e in export["manifest"])


def test_ping_reports_transfer_capabilities(agent):
    ping = agent.dispatch({"type": "ping"})
    assert ping["workspace_git"] == 1
    assert ping["workspace_chunk"] == 2 * 1024 * 1024
    assert ping["session_transfer"] == 1


def test_legacy_memory_dir_matches_claude_project_naming(agent):
    folder = agent.root / "sales_bot.v2"
    folder.mkdir()
    name = agent.legacy_memory_dir(folder).parent.name
    assert name == str(folder).replace("/", "-").replace("_", "-").replace(".", "-")
    assert agent.memory_dir(str(folder)) == folder / ".coreman-memory"


def test_claude_session_files_move_to_new_directory(agent):
    call(agent, "init", "one")
    call(agent, "init", "two")
    sid, other = str(uuid.uuid4()), str(uuid.uuid4())
    project = agent.claude_project_dir(str(agent.root / "one"))
    project.mkdir(parents=True, exist_ok=True)
    (project / f"{sid}.jsonl").write_text('{"type":"user"}\n' * 1000)
    (project / sid / "subagents").mkdir(parents=True)
    (project / sid / "subagents/a.jsonl").write_text("{}\n")
    (agent.sessions_dir / sid / "files").mkdir(parents=True)
    (agent.sessions_dir / sid / "claude_usage.json").write_text('{"input": 1}')
    listing = agent.dispatch(
        {
            "type": "session-export",
            "working_dir": "one",
            "bot_id": "b1",
            "sessions": [sid, other],
        }
    )
    files = {(f["area"], f["path"]) for f in listing["files"]}
    assert files == {
        ("project", f"{sid}.jsonl"),
        ("project", f"{sid}/subagents/a.jsonl"),
        ("relay", f"{sid}/claude_usage.json"),
    }
    transfer = str(uuid.uuid4())
    for item in listing["files"]:
        chunk = agent.dispatch(
            {
                "type": "session-read",
                "working_dir": "one",
                "bot_id": "b1",
                "area": item["area"],
                "path": item["path"],
            }
        )
        result = agent.dispatch(
            {
                "type": "session-import",
                "working_dir": "two",
                "bot_id": "b1",
                "transfer_id": transfer,
                "area": item["area"],
                "path": item["path"],
                "offset": 0,
                "size": item["size"],
                "hash": item["hash"],
                "data": chunk["data"],
            }
        )
        assert result["installed"]
    moved = agent.claude_project_dir(str(agent.root / "two"))
    assert (moved / f"{sid}.jsonl").read_text() == (project / f"{sid}.jsonl").read_text()
    assert (moved / sid / "subagents/a.jsonl").exists()


def test_session_paths_and_hash_are_checked(agent):
    call(agent, "init", "one")
    base = {"working_dir": "one", "bot_id": "b1", "transfer_id": str(uuid.uuid4())}
    for area, path in [
        ("project", "../x.jsonl"),
        ("project", "memory/a.md"),
        ("relay", "not-a-session/x"),
        ("other", f"{uuid.uuid4()}.jsonl"),
    ]:
        with pytest.raises(OperationError):
            agent.dispatch({"type": "session-read", **base, "area": area, "path": path})
    raw = b"hello"
    with pytest.raises(OperationError):
        agent.dispatch(
            {
                "type": "session-import",
                **base,
                "area": "project",
                "path": f"{uuid.uuid4()}.jsonl",
                "offset": 0,
                "size": len(raw),
                "hash": hashlib.sha256(b"other").hexdigest(),
                "data": base64.b64encode(raw).decode(),
            }
        )
    # 别的员工的目录不能读写会话。
    (agent.root / "one/.coreman-workspace.json").write_text('{"bot_id": "b2"}')
    with pytest.raises(OperationError):
        agent.dispatch({"type": "session-export", **base, "sessions": []})


@pytest.fixture
def codex_agent(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    root = tmp_path / "workspaces"
    root.mkdir()
    return Agent(
        root=root,
        home=tmp_path / "home",
        api_url="https://example.com",
        token="synthetic-test-token-123456789",
        relay_id="test",
        provider="codex",
        sessions_dir=tmp_path / "relay-sessions",
    )


def test_codex_ping_and_session_areas(codex_agent, tmp_path, monkeypatch):
    ping = codex_agent.dispatch({"type": "ping"})
    assert ping["session_transfer_codex"] == 1 and ping["session_transfer"] == 0
    assert codex_agent.codex_sessions_dir() == tmp_path / "home/.codex/sessions"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    assert codex_agent.codex_sessions_dir() == tmp_path / "codex-home/sessions"


def test_codex_rollout_paths_are_checked(codex_agent):
    call(codex_agent, "init", "one")
    base = {"working_dir": "one", "bot_id": "b1"}
    thread = str(uuid.uuid4())
    for area, path in [
        ("codex", f"2026/10/21/rollout-x-{thread}.jsonl"),
        ("codex", f"2026/10/rollout-2026-10-21T08-00-00-{thread}.jsonl"),
        ("codex", f"../2026/10/21/rollout-2026-10-21T08-00-00-{thread}.jsonl"),
        ("codex", "2026/10/21/rollout-2026-10-21T08-00-00-not-a-thread.jsonl"),
        ("project", f"{thread}.jsonl"),
    ]:
        with pytest.raises(OperationError):
            codex_agent.dispatch({"type": "session-read", **base, "area": area, "path": path})


def test_codex_export_needs_a_bound_unique_rollout(codex_agent):
    call(codex_agent, "init", "one")
    sessions = codex_agent.codex_sessions_dir()
    bound, unbound, linked = (str(uuid.uuid4()) for _ in range(3))
    thread, other = str(uuid.uuid4()), str(uuid.uuid4())
    rollout = f"2026/10/21/rollout-2026-10-21T08-00-00-{thread}.jsonl"
    (sessions / rollout).parent.mkdir(parents=True)
    (sessions / rollout).write_text("{}\n")
    elsewhere = sessions.parent / f"rollout-2026-10-21T08-00-00-{other}.jsonl"
    elsewhere.write_text("{}\n")
    (sessions / f"2026/10/21/rollout-2026-10-21T09-00-00-{other}.jsonl").symlink_to(elsewhere)
    for sid, value in [(bound, thread), (linked, other)]:
        (codex_agent.sessions_dir / sid).mkdir(parents=True)
        (codex_agent.sessions_dir / sid / "codex_thread.txt").write_text(value + "\n")
    listing = codex_agent.dispatch(
        {
            "type": "session-export",
            "working_dir": "one",
            "bot_id": "b1",
            "sessions": [bound, unbound, linked],
        }
    )
    assert {(f["session"], f["area"], f["path"]) for f in listing["files"]} == {
        (bound, "codex", rollout),
        (bound, "relay", f"{bound}/codex_thread.txt"),
    }

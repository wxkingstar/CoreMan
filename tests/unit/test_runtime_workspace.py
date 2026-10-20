import base64
import hashlib
import os
import uuid

import pytest

from runtime_daemon.agent import Agent, OperationError


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
    )


def call(agent, kind, **kw):
    return agent.dispatch({"type": "workspace-" + kind, "working_dir": "one", "bot_id": 1, **kw})


def test_init_preserves_instructions(agent):
    p = agent.root / "one"
    p.mkdir()
    (p / "AGENTS.md").write_text("original")
    (p / "CLAUDE.md").write_text("conflict")
    assert call(agent, "init", content="new")["code"] == "instructions_conflict"
    assert (p / "CLAUDE.md").read_text() == "conflict"
    (p / "CLAUDE.md").write_text("original")
    call(agent, "init", content="new")
    assert (p / "AGENTS.md").read_text() == "original"
    assert (p / "CLAUDE.md").readlink().as_posix() == "AGENTS.md"
    assert (p / "CLAUDE.md.preserved-1").read_text() == "original"
    assert call(agent, "info")["owned"]


def shared_workspace(agent):
    """另一套机器人系统的目录：Git 跟踪的 CLAUDE.md 为正本，AGENTS.md 链接过去，组可写共用。"""
    p = agent.root / "one"
    (p / ".git/info").mkdir(parents=True)
    (p / "CLAUDE.md").write_text("# shared rules\n")
    (p / "CLAUDE.md").chmod(0o664)
    (p / "AGENTS.md").symlink_to("CLAUDE.md")
    (p / "report.csv").write_text("a,b\n")
    return p


def test_init_takes_over_shared_workspace_without_changing_layout(agent):
    p = shared_workspace(agent)
    info = call(agent, "info")
    assert info["exists"] and not info["empty"] and not info["owned"] and not info["marked"]
    call(agent, "init", content="new")
    assert (p / "CLAUDE.md").read_text() == "# shared rules\n"
    assert not (p / "CLAUDE.md").is_symlink()
    assert (p / "AGENTS.md").readlink().as_posix() == "CLAUDE.md"
    assert not list(p.glob("CLAUDE.md.preserved-*"))
    # 对方部署时 git clean -fd 不带 -x：标记列入本地排除才不会被删。
    assert (p / ".git/info/exclude").read_text() == "/.coreman-workspace.json\n"
    info = call(agent, "info")
    assert info["owned"] and info["marked"]
    other = agent.dispatch({"type": "workspace-info", "working_dir": "one", "bot_id": 2})
    assert other["marked"] and not other["owned"]


def test_init_links_agents_md_to_lone_claude_md(agent):
    p = agent.root / "one"
    p.mkdir()
    (p / "CLAUDE.md").write_text("tracked rules")
    call(agent, "init", content="new")
    assert (p / "CLAUDE.md").read_text() == "tracked rules"
    assert not (p / "CLAUDE.md").is_symlink()
    assert (p / "AGENTS.md").readlink().as_posix() == "CLAUDE.md"
    assert not list(p.glob("CLAUDE.md.preserved-*"))


def test_edit_through_agents_link_keeps_shared_permissions(agent):
    import os
    import stat

    p = shared_workspace(agent)
    call(agent, "init")
    original = call(agent, "read", path="AGENTS.md")
    assert original["editable"]
    call(agent, "write", path="AGENTS.md", content="updated", expected_hash=original["hash"])
    assert (p / "CLAUDE.md").read_text() == "updated"
    assert (p / "AGENTS.md").is_symlink()
    assert stat.S_IMODE((p / "CLAUDE.md").stat().st_mode) == 0o664
    # 新文件按 umask 取权限，而不是收成 0600。
    call(agent, "write", path="notes.md", content="x", expected_hash=None)
    mask = os.umask(0)
    os.umask(mask)
    assert stat.S_IMODE((p / "notes.md").stat().st_mode) == 0o666 & ~mask
    assert not list(p.glob(".coreman-tmp-*"))


def test_file_bounds_and_hash(agent):
    call(agent, "init")
    result = call(agent, "write", path="a.txt", content="hello", expected_hash=None)
    assert call(agent, "read", path="a.txt")["content"] == "hello"
    assert (
        call(agent, "write", path="a.txt", content="bad", expected_hash="stale")["code"]
        == "conflict"
    )
    with pytest.raises(OperationError):
        call(agent, "read", path="../escape")
    (agent.root / "one" / "escape").symlink_to("/etc/passwd")
    with pytest.raises(OperationError):
        call(agent, "read", path="escape")
    assert (
        call(agent, "write", path="a.txt", content="ok", expected_hash=result["hash"])["size"] == 2
    )


def test_transfer_hash_and_nonempty_target(agent):
    call(agent, "init")
    call(agent, "write", path="a.txt", content="hello", expected_hash=None)
    manifest = call(agent, "export")["manifest"]
    transfer = str(uuid.uuid4())
    call(agent, "import", working_dir="two", transfer_id=transfer, manifest=manifest)
    for entry in manifest:
        if "link" not in entry:
            data = call(agent, "read", path=entry["path"])["data"]
            call(
                agent,
                "import",
                working_dir="two",
                transfer_id=transfer,
                path=entry["path"],
                data=data,
                offset=0,
            )
    call(agent, "finish", working_dir="two", transfer_id=transfer)
    assert (agent.root / "two" / "a.txt").read_text() == "hello"
    assert call(agent, "finish", working_dir="two", transfer_id=transfer)["installed"]


def test_bad_manifest_and_secrets(agent):
    call(agent, "init")
    (agent.root / "one" / ".env").write_text("SECRET=x")
    assert ".env" not in [x["path"] for x in call(agent, "export")["manifest"]]
    with pytest.raises(OperationError):
        call(agent, "read", path=".env")
    with pytest.raises(OperationError):
        call(
            agent,
            "import",
            working_dir="two",
            transfer_id=str(uuid.uuid4()),
            manifest=[{"path": "../escape", "size": 1, "hash": "a" * 64, "mode": 420}],
        )


def test_transfer_rejects_corruption_and_preserves_target(agent):
    transfer = str(uuid.uuid4())
    manifest = [
        {"path": "a.txt", "size": 3, "hash": hashlib.sha256(b"yes").hexdigest(), "mode": 420}
    ]
    call(
        agent,
        "import",
        transfer_id=transfer,
        manifest=manifest,
        path="a.txt",
        offset=0,
        data=base64.b64encode(b"bad").decode(),
    )
    with pytest.raises(OperationError):
        call(agent, "finish", transfer_id=transfer)
    assert not (agent.root / "one").exists()


def test_canonical_alias_edit_and_other_link_block(agent):
    call(agent, "init")
    original = call(agent, "read", path="CLAUDE.md")
    call(agent, "write", path="CLAUDE.md", content="updated", expected_hash=original["hash"])
    assert (agent.root / "one" / "AGENTS.md").read_text() == "updated"
    (agent.root / "one" / "alias").symlink_to("AGENTS.md")
    with pytest.raises(OperationError):
        call(agent, "write", path="alias", content="bad", expected_hash=original["hash"])


def test_bundle_includes_workspace_module():
    from runtime_daemon.build import ROOT, source_files

    assert ROOT / "runtime_daemon/workspace.py" in source_files(ROOT)


def test_links_cannot_reveal_excluded_secrets(agent):
    call(agent, "init")
    (agent.root / "one" / ".env").write_text("SECRET")
    (agent.root / "one" / "innocent.txt").symlink_to(".env")
    with pytest.raises(OperationError):
        call(agent, "read", path="innocent.txt")


def test_git_rejects_executable_repo_config(agent, monkeypatch):
    import os
    import subprocess

    monkeypatch.setenv("PATH", "/opt/homebrew/bin:" + os.environ["PATH"])
    call(agent, "init")
    subprocess.run(["git", "init", str(agent.root / "one")], check=True, capture_output=True)
    config = agent.root / "one" / ".git" / "config"
    with config.open("a") as f:
        f.write('\n[filter "evil"]\n\tclean = touch /tmp/should-not-run\n')
    with pytest.raises(OperationError):
        call(agent, "git-status")


def test_configured_workspace_symlink_and_owner_mismatch(agent):
    call(agent, "init")
    (agent.root / "alias").symlink_to("one")
    with pytest.raises(OperationError):
        call(agent, "list", working_dir="alias")
    with pytest.raises(OperationError):
        call(agent, "list", bot_id=2)
    assert not call(agent, "info", bot_id=2)["owned"]


def test_finish_retry_after_destination_conflict(agent):
    manifest = [
        {"path": "AGENTS.md", "size": 0, "hash": hashlib.sha256(b"").hexdigest(), "mode": 420},
        {"path": "CLAUDE.md", "link": "AGENTS.md"},
    ]
    transfer = str(uuid.uuid4())
    call(agent, "import", transfer_id=transfer, manifest=manifest)
    root = agent.root / "one"
    root.mkdir()
    (root / "existing").write_text("keep")
    with pytest.raises(OperationError):
        call(agent, "finish", transfer_id=transfer)
    assert (root / "existing").read_text() == "keep"
    (root / "existing").unlink()
    assert call(agent, "finish", transfer_id=transfer)["installed"]
    assert call(agent, "finish", transfer_id=transfer)["installed"]


def copy_to(agent, target, **finish):
    manifest = call(agent, "export")["manifest"]
    transfer = str(uuid.uuid4())
    call(agent, "import", working_dir=target, transfer_id=transfer, manifest=manifest)
    for entry in manifest:
        if "link" not in entry:
            data = call(agent, "read", path=entry["path"], transfer_id=transfer)["data"]
            call(
                agent,
                "import",
                working_dir=target,
                transfer_id=transfer,
                path=entry["path"],
                data=data,
                offset=0,
            )
    call(agent, "finish", working_dir=target, transfer_id=transfer, **finish)
    return manifest


def test_transfer_replaces_target_and_carries_git(agent):
    assert agent.dispatch({"type": "ping"})["workspace_replace"] is True
    call(agent, "init")
    source = agent.root / "one"
    (source / "a.txt").write_text("hello")
    (source / ".git/objects/ab").mkdir(parents=True)
    (source / ".git/HEAD").write_text("ref: refs/heads/main\n")
    (source / ".git/objects/ab/cd").write_bytes(b"object")
    (source / ".git/objects/ab/cd").chmod(0o444)
    (source / ".env").write_text("SECRET=x")
    target = agent.root / "two"
    (target / "node_modules").mkdir(parents=True)
    (target / "node_modules/x.js").write_text("old")
    (target / "old.txt").write_text("old")
    (target / ".git").mkdir()
    (target / ".git/old").write_text("old")

    manifest = copy_to(agent, "two", replace=True)
    assert ".git/HEAD" in [entry["path"] for entry in manifest]
    assert (target / "a.txt").read_text() == "hello"
    assert not (target / "old.txt").exists() and not (target / "node_modules").exists()
    assert (target / ".git/HEAD").read_text() == "ref: refs/heads/main\n"
    assert (target / ".git/objects/ab/cd").read_bytes() == b"object"
    assert not (target / ".git/old").exists() and not (target / ".env").exists()
    assert call(agent, "info", working_dir="two")["owned"]
    assert not [p for p in agent.root.iterdir() if p.name.startswith(".coreman-transfer-")]
    # 文件管理仍看不到 .git，只有带迁移标识的读取能取到。
    with pytest.raises(OperationError):
        call(agent, "read", path=".git/HEAD")
    assert ".git" not in [entry["name"] for entry in call(agent, "list")["entries"]]


def test_transfer_never_replaces_another_employees_directory(agent):
    call(agent, "init")
    call(agent, "init", working_dir="two", bot_id=2)
    (agent.root / "two" / "theirs.txt").write_text("keep")
    with pytest.raises(OperationError, match="其他员工"):
        copy_to(agent, "two", replace=True)
    assert (agent.root / "two" / "theirs.txt").read_text() == "keep"


def test_memory_operations_enforce_supplied_owner(agent):
    call(agent, "init")
    with pytest.raises(OperationError):
        agent.dispatch({"type": "read-memory", "working_dir": "one", "bot_id": 2})
    with pytest.raises(OperationError):
        agent.dispatch(
            {
                "type": "deploy-memory",
                "bot_id": 2,
                "memories": [{"working_dir": "one", "file_name": "MEMORY.md", "content": "bad"}],
            }
        )
    assert agent.dispatch({"type": "read-memory", "working_dir": "one", "bot_id": 1})["success"]


def test_init_and_prepare_remove_only_legacy_instruction_excludes(agent):
    root = agent.root / "one"
    (root / ".git/info").mkdir(parents=True)
    exclude = root / ".git/info/exclude"
    exclude.write_text("# keep\n/AGENTS.md\nAGENTS.md\nsecret.txt\n")
    call(agent, "init")
    assert exclude.read_text() == "# keep\nsecret.txt\n/.coreman-workspace.json\n"
    call(agent, "init")
    assert exclude.read_text().count("/.coreman-workspace.json") == 1
    exclude.write_text("/AGENTS.md\ncache/\n")
    agent.prepare_workspace(root)
    assert exclude.read_text() == "cache/\n"


def test_git_cancellation_stops_subprocess(agent, tmp_path, monkeypatch):
    import os
    import subprocess
    import threading

    from runtime_daemon.agent import COMMAND_CANCEL
    from runtime_daemon.workspace import Workspace

    call(agent, "init")
    binary = tmp_path / "bin"
    binary.mkdir()
    fake = binary / "git"
    fake.write_text("#!/bin/sh\nexec sleep 30\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary) + ":" + os.environ["PATH"])
    spawned = []
    original = subprocess.Popen

    def capture(*args, **kwargs):
        process = original(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", capture)
    cancel = threading.Event()
    cancel.set()
    context = COMMAND_CANCEL.set(cancel)
    try:
        with pytest.raises(OperationError):
            Workspace(
                agent, {"type": "workspace-git-status", "working_dir": "one", "bot_id": 1}
            ).git(["status"])
    finally:
        COMMAND_CANCEL.reset(context)
    assert spawned and all(p.poll() is not None for p in spawned)


def test_init_links_claude_skills_to_agents_skills(agent):
    call(agent, "init")
    p = agent.root / "one"
    assert (p / ".agents/skills").is_dir()
    assert os.readlink(p / ".claude/skills") == "../.agents/skills"


def test_init_replaces_per_skill_links_with_directory_link(agent):
    p = agent.root / "one"
    (p / ".agents/skills/query").mkdir(parents=True)
    (p / ".claude/skills").mkdir(parents=True)
    (p / ".claude/skills/query").symlink_to("../../.agents/skills/query")
    call(agent, "init")
    assert os.readlink(p / ".claude/skills") == "../.agents/skills"
    assert (p / ".agents/skills/query").is_dir()


def test_init_keeps_claude_only_skills(agent):
    p = agent.root / "one"
    (p / ".claude/skills/own").mkdir(parents=True)
    call(agent, "init")
    assert not (p / ".claude/skills").is_symlink()
    assert (p / ".claude/skills/own").is_dir()


def test_init_leaves_git_repository_skills_layout(agent):
    shared_workspace(agent)
    call(agent, "init")
    assert not (agent.root / "one/.claude").exists()
    assert not (agent.root / "one/.gitignore").exists()


def test_init_writes_gitignore_that_keeps_skills_and_mcp(agent):
    import subprocess

    call(agent, "init")
    p = agent.root / "one"
    (p / ".mcp.json").write_text("{}")
    (p / ".claude/settings.local.json").write_text("{}")
    (p / ".env").write_text("A=1\n")
    subprocess.run(["git", "init", "-q"], cwd=p, check=True)
    ignored = subprocess.run(
        [
            "git",
            "-c",
            "core.excludesFile=/dev/null",
            "check-ignore",
            "--no-index",
            ".mcp.json",
            ".agents/skills",
            ".claude/skills",
            "AGENTS.md",
            ".claude/settings.local.json",
            ".env",
            ".coreman-workspace.json",
        ],
        cwd=p,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert ignored == [".claude/settings.local.json", ".env", ".coreman-workspace.json"]


def test_init_keeps_existing_gitignore(agent):
    p = agent.root / "one"
    p.mkdir()
    (p / ".gitignore").write_text("own\n")
    call(agent, "init")
    assert (p / ".gitignore").read_text() == "own\n"


def test_skill_links_accept_directory_link(agent):
    call(agent, "init")
    p = agent.root / "one"
    (p / ".agents/skills/query").mkdir()
    (p / ".agents/skills/query/SKILL.md").write_text("# query\n")
    agent.link_claude_skills(p, "query")
    assert os.readlink(p / ".claude/skills") == "../.agents/skills"
    assert (p / ".claude/skills/query/SKILL.md").is_file()


def test_git_view_reports_changes_and_diffs_read_only(agent, tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setenv("PATH", "/opt/homebrew/bin:" + os.environ["PATH"])
    call(agent, "init")
    assert call(agent, "git-status") == {"success": True, "initialized": False}
    root = agent.root / "one"
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=root, check=True)
    (root / "notes.md").write_text("one\ntwo\n")
    subprocess.run([*git, "add", "notes.md"], cwd=root, check=True)
    subprocess.run([*git, "commit", "-qm", "first"], cwd=root, check=True)
    subprocess.run([*git, "remote", "add", "origin", str(bare)], cwd=root, check=True)
    subprocess.run([*git, "push", "-q", "-u", "origin", "main"], cwd=root, check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "local"], cwd=root, check=True)
    # 令牌写在 origin 里的不能原样展示；仓库自带的外部 diff 程序不能被执行。
    subprocess.run(
        [*git, "remote", "set-url", "origin", "https://bot:secret@git.example.com/org/r.git"],
        cwd=root,
        check=True,
    )
    marker = tmp_path / "ext-diff-ran"
    subprocess.run([*git, "config", "diff.external", f"touch {marker}"], cwd=root, check=True)
    (root / "notes.md").write_text("one\nthree\n")
    (root / "new.txt").write_text("hello\n")
    (root / ".env").write_text("SECRET=1")
    (root / "drafts").mkdir()
    (root / "drafts" / "a.md").write_text("a")
    status = call(agent, "git-status")
    assert status["initialized"] and status["branch"] == "main"
    assert status["remote"] == "https://git.example.com/org/r.git"
    assert status["upstream"] == "origin/main" and status["ahead"] == 1
    assert status["head"]["subject"] == "local"
    files = {e["path"]: e["status"] for e in status["files"]}
    assert files["notes.md"] == " M" and files["new.txt"] == "??" and files["drafts/"] == "??"
    assert ".env" not in files
    changed = call(agent, "git-diff", path="notes.md")["diff"]
    assert "-two" in changed and "+three" in changed and not marker.exists()
    assert "+hello" in call(agent, "git-diff", path="new.txt")["diff"]
    with pytest.raises(OperationError):
        call(agent, "git-diff", path=".env")


def test_probe_finds_file_only_through_the_same_directory(agent, tmp_path):
    """共享盘上的两台节点：一台建的随机名文件另一台在同一路径找得到；独立目录找不到。"""
    shared = Agent(
        root=agent.root,
        home=tmp_path / "home2",
        api_url="https://example.com",
        token="synthetic-test-token-123456789",
        relay_id="peer",
    )
    elsewhere_root = tmp_path / "elsewhere"
    elsewhere_root.mkdir()
    elsewhere = Agent(
        root=elsewhere_root,
        home=tmp_path / "home3",
        api_url="https://example.com",
        token="synthetic-test-token-123456789",
        relay_id="other",
    )
    assert agent.dispatch({"type": "ping"})["workspace_probe"] == 1
    call(agent, "init")
    call(elsewhere, "init")
    token = uuid.uuid4().hex
    assert call(agent, "probe", token=token, action="create")["created"]
    assert call(elsewhere, "probe", token=token, action="check")["found"] is False
    assert call(shared, "probe", token=token, action="check")["found"] is True
    # 目标只查不删，由建文件的源节点删除。
    assert list((agent.root / "one").glob(".coreman-probe-*"))
    call(agent, "probe", token=token, action="remove")
    assert not list((agent.root / "one").glob(".coreman-probe-*"))
    assert call(shared, "probe", token=token, action="check")["found"] is False


def test_probe_rejects_bad_token_missing_directory_and_other_employee(agent):
    token = uuid.uuid4().hex
    with pytest.raises(OperationError, match="令牌"):
        call(agent, "probe", token="../x", action="create")
    with pytest.raises(OperationError, match="不存在"):
        call(agent, "probe", token=token, action="create")
    assert call(agent, "probe", token=token, action="check")["found"] is False
    call(agent, "init", working_dir="two", bot_id=2)
    with pytest.raises(OperationError, match="其他员工"):
        call(agent, "probe", working_dir="two", token=token, action="create")

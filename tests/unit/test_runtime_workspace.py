import base64
import hashlib
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


def test_git_backup_status_and_no_hooks(agent, tmp_path, monkeypatch):
    import os
    import subprocess

    monkeypatch.setenv("PATH", "/opt/homebrew/bin:" + os.environ["PATH"])
    call(agent, "init")
    call(agent, "write", path="a.txt", content="hello", expected_hash=None)
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    # Test transport uses local bare remote by substituting only the validated remote and
    # protocol setting. Production rejects file transport unconditionally.
    from runtime_daemon.workspace import Workspace

    original = Workspace.git

    def local_git(self, args, cwd=None, token=""):
        args = [str(bare) if a == "https://github.com/example/repo.git" else a for a in args]
        if args[0] in {"push", "fetch", "ls-remote", "clone"}:
            args = ["-c", "protocol.file.allow=always", *args]
        result = original(self, args, cwd, token)
        return (
            "https://github.com/example/repo.git"
            if args == ["remote", "get-url", "origin"]
            else result
        )

    monkeypatch.setattr(Workspace, "git", local_git)
    config = {"git_url": "https://github.com/example/repo.git", "branch": "main"}
    result = call(agent, "git-backup", files=["AGENTS.md", "CLAUDE.md", "a.txt"], **config)
    assert result["pushed"]
    assert call(agent, "git-status")["head"] == result["head"]
    hook = agent.root / "one" / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\ntouch " + str(tmp_path / "hook-ran") + "\n")
    hook.chmod(0o755)
    (agent.root / "one" / "a.txt").unlink()
    call(agent, "git-backup", files=["a.txt"], **config)
    assert not (tmp_path / "hook-ran").exists()
    assert not call(agent, "git-status")["files"]
    assert call(agent, "git-backup", files=[], **config)["pushed"]
    call(agent, "init", working_dir="two")
    call(agent, "write", working_dir="two", path="new.txt", content="new", expected_hash=None)
    assert call(agent, "git-status", working_dir="two")["files"]
    assert call(agent, "git-backup", working_dir="two", files=["new.txt"], **config)["pushed"]


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


def test_git_status_reports_excluded_secrets(agent):
    call(agent, "init")
    (agent.root / "one" / ".env").write_text("SECRET")
    assert ".env" in call(agent, "git-status")["excluded"]


def test_init_and_prepare_remove_only_legacy_instruction_excludes(agent):
    root = agent.root / "one"
    (root / ".git/info").mkdir(parents=True)
    exclude = root / ".git/info/exclude"
    exclude.write_text("# keep\n/AGENTS.md\nAGENTS.md\nsecret.txt\n")
    call(agent, "init")
    assert exclude.read_text() == "# keep\nsecret.txt\n"
    exclude.write_text("/AGENTS.md\ncache/\n")
    agent.prepare_workspace(root)
    assert exclude.read_text() == "cache/\n/.claude/output-styles/\n"


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


def test_git_backup_pathspec_is_literal(agent, tmp_path, monkeypatch):
    import os
    import subprocess

    from runtime_daemon.workspace import Workspace

    monkeypatch.setenv("PATH", "/opt/homebrew/bin:" + os.environ["PATH"])
    call(agent, "init")
    root = agent.root / "one"
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    (root / ".env").write_text("secret")
    original = Workspace.git

    def local_git(self, args, cwd=None, token=""):
        if args[0] == "ls-remote":
            return ""
        return original(self, args, cwd, token)

    monkeypatch.setattr(Workspace, "git", local_git)
    for pattern in ["*", ":(glob)**"]:
        with pytest.raises(OperationError):
            call(
                agent,
                "git-backup",
                files=[pattern],
                git_url="https://github.com/example/repo.git",
                branch="main",
            )
        staged = subprocess.run(
            ["git", "ls-files"], cwd=root, check=True, capture_output=True, text=True
        ).stdout
        assert not staged

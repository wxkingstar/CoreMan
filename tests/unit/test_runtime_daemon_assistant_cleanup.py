"""守护进程收拾助理留下的东西：保留目录不给助理用、回收站清扫、改回普通实例后的本轮目录。"""

from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from runtime_daemon import agent as agent_module
from runtime_daemon import daemon as module
from runtime_daemon import workspace
from runtime_daemon.daemon import Daemon


@pytest.fixture
def node(tmp_path):
    root = tmp_path / "projects"
    (root / "bot-1").mkdir(parents=True)
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"workspace_root": str(root), "api_url": "http://localhost", "node_id": "n1"})
    )
    daemon = Daemon(path)
    daemon.socket_dir = Path(tempfile.mkdtemp(dir="/tmp"))
    daemon.agents["claude"] = agent_module.Agent(
        root=root,
        home=tmp_path / "home",
        api_url="http://localhost",
        token="synthetic-test-token-123456789",
        relay_id="relay-1",
    )
    return daemon


async def execute(daemon: Daemon, path: str, body: dict) -> list[dict]:
    sent: list[dict] = []

    async def send_frames(identity, frames):
        sent.extend(frames)

    daemon.send_frames = send_frames
    await daemon.execute(
        {
            "id": "call-1",
            "provider": "claude",
            "method": "POST",
            "path": path,
            "body": base64.b64encode(json.dumps(body).encode()).decode(),
        }
    )
    return sent


def code_of(frames: list[dict]) -> str:
    assert frames[0]["status_code"] == 403
    return json.loads(base64.b64decode(frames[0]["data"]))["error"]["code"]


ENTRY = "0123456789abcdef0123456789abcdef"  # Workspace.remove 写进回收站的名字：32 位小写十六进制

SANDBOXED = {"sandbox": True, "transcript": {"transfer": "0" * 32, "restore": False}}


@pytest.mark.parametrize(
    "directory",
    [".coreman-health", ".coreman-health/sub", ".coreman-trash/x", ".coreman-owners"],
)
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/", {"type": "workspace-init"}),
        ("/", {"type": "workspace-info"}),
        ("/", {"type": "workspace-remove"}),
        ("/v1/chat/completions", {**SANDBOXED, "messages": []}),
    ],
)
async def test_assistant_only_refuses_reserved_directories(
    node, monkeypatch, directory, path, body
):
    node.assistant_only = True

    def no_dispatch(data):
        raise AssertionError("refused before reaching the agent")

    async def no_run(*_args):
        raise AssertionError("refused before reaching the sandbox")

    monkeypatch.setattr(node.agents["claude"], "dispatch", no_dispatch)
    monkeypatch.setattr(node.assistant, "run", no_run)
    frames = await execute(node, path, {**body, "working_dir": directory, "bot_id": "b1"})
    assert code_of(frames) == "reserved_directory"


async def test_an_ordinary_daemon_does_not_check_reserved_directories(node, monkeypatch):
    seen: list = []

    def dispatch(data):
        seen.append(data["working_dir"])
        return {"success": True}

    monkeypatch.setattr(node.agents["claude"], "dispatch", dispatch)
    frames = await execute(node, "/", {"type": "workspace-info", "working_dir": ".coreman-health"})
    assert seen == [".coreman-health"] and frames[0]["status_code"] == 200


def test_the_allow_list_includes_the_directory_removal():
    assert "workspace-remove" in module.ASSISTANT_ONLY_OPERATIONS
    assert module.assistant_only_refusal("POST", "/", {"type": "workspace-remove"}) is None


def test_runtime_agents_carry_the_assistant_only_flag(node):
    node.config["node_token"] = "synthetic-node-token"
    node.assistant_only = True
    assert module.RuntimeAgent(node, "claude", "relay-1").assistant_only is True
    node.assistant_only = False
    assert module.RuntimeAgent(node, "claude", "relay-1").assistant_only is False


async def test_the_sweep_empties_the_trash_without_following_links(node, tmp_path):
    root = Path(node.config["workspace_root"])
    trash = workspace.trash_dir(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    (trash / "leftover" / "deep").mkdir(parents=True)
    (trash / "leftover" / "deep" / "f").write_text("x")
    (trash / "leftover" / "link").symlink_to(outside)
    (trash / "stray-link").symlink_to(outside)
    await node.assistant.sweep()
    assert list(trash.iterdir()) == []
    assert (outside / "keep").read_text() == "x"


async def test_the_sweep_leaves_a_trash_that_is_not_a_real_directory(node, tmp_path):
    root = Path(node.config["workspace_root"])
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "precious").mkdir(parents=True)
    (root / workspace.TRASH_DIR).symlink_to(elsewhere)
    await node.assistant.sweep()
    assert (elsewhere / "precious").is_dir()


def wait_until(check) -> None:
    for _ in range(100):
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_an_ordinary_daemon_clears_assistant_turn_leftovers_once(node, tmp_path):
    turns = node.assistant.turns_dir
    (turns / "abc" / "config").mkdir(parents=True)
    (turns / "abc" / "config" / "transcript.jsonl").write_text("plaintext")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    (turns / "abc" / "link").symlink_to(outside)
    node.start_leftover_cleanup()
    wait_until(lambda: not os.path.lexists(turns))
    assert (outside / "keep").read_text() == "x"


def test_an_ordinary_daemon_unlinks_a_linked_turns_directory(node, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    turns = node.assistant.turns_dir
    turns.parent.mkdir(parents=True, exist_ok=True)
    turns.symlink_to(outside)
    node.start_leftover_cleanup()
    wait_until(lambda: not os.path.lexists(turns))
    assert (outside / "keep").read_text() == "x"


def test_a_failing_cleanup_is_contained(node, monkeypatch):
    def boom(_path):
        raise RuntimeError("disk on fire")

    (node.assistant.turns_dir / "x").mkdir(parents=True)
    monkeypatch.setattr(module, "remove_tree", boom)
    node.clear_assistant_leftovers()  # 不抛
    assert node.assistant.turns_dir.is_dir()


def test_an_assistant_only_daemon_does_not_clear_its_own_turns(node):
    node.assistant_only = True
    (node.assistant.turns_dir / "live").mkdir(parents=True)
    trash = workspace.trash_dir(Path(node.config["workspace_root"]))
    (trash / ENTRY).mkdir()
    node.start_leftover_cleanup()
    time.sleep(0.3)
    assert (node.assistant.turns_dir / "live").is_dir()
    assert (trash / ENTRY).is_dir()


def test_an_ordinary_daemon_empties_the_trash_without_following_links(node, tmp_path):
    root = Path(node.config["workspace_root"])
    trash = workspace.trash_dir(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    (trash / ENTRY / "deep").mkdir(parents=True)
    (trash / ENTRY / "deep" / "f").write_text("plaintext")
    (trash / ENTRY / "link").symlink_to(outside)
    node.clear_assistant_leftovers()
    assert list(trash.iterdir()) == []
    assert (outside / "keep").read_text() == "x"


def test_an_ordinary_daemon_leaves_a_linked_trash_alone(node, tmp_path):
    root = Path(node.config["workspace_root"])
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "precious").mkdir(parents=True)
    (root / workspace.TRASH_DIR).symlink_to(elsewhere)
    node.clear_assistant_leftovers()
    assert (root / workspace.TRASH_DIR).is_symlink()
    assert (elsewhere / "precious").is_dir()


def test_an_ordinary_start_assistant_does_not_clean_a_second_time(node, monkeypatch):
    calls: list = []
    monkeypatch.setattr(node, "clear_assistant_leftovers", lambda: calls.append(True))
    node.start_assistant()
    time.sleep(0.1)
    assert calls == []


async def test_leftovers_are_cleared_even_when_enrolment_fails(node, monkeypatch):
    """令牌被吊销的节点每次启动都在登记处退出：清理在那之前就已开始。"""
    turns = node.assistant.turns_dir
    (turns / "abc").mkdir(parents=True)
    (turns / "abc" / "transcript.jsonl").write_text("plaintext")
    trash = workspace.trash_dir(Path(node.config["workspace_root"]))
    (trash / ENTRY).mkdir()
    (trash / ENTRY / "notes.md").write_text("plaintext")
    socket_dir = Path(tempfile.mkdtemp(prefix="cm-", dir="/tmp")) / "run"
    monkeypatch.setattr(module, "choose_socket_dir", lambda data_dir, node_id: socket_dir)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    calls: list = []
    original = node.clear_assistant_leftovers

    def counted():
        calls.append(True)
        original()

    monkeypatch.setattr(node, "clear_assistant_leftovers", counted)

    async def revoked():
        raise RuntimeError("token revoked")

    monkeypatch.setattr(node, "enroll", revoked)
    try:
        with pytest.raises(RuntimeError):
            await node.run()
        wait_until(lambda: not os.path.lexists(turns) and not any(trash.iterdir()))
        assert calls == [True]
    finally:
        node.log_rotator.stop()
        node.lock_file.close()
        shutil.rmtree(socket_dir.parent, ignore_errors=True)


def ordinary_trash(node):
    """普通实例上 `.coreman-trash` 不是保留名：它可以是员工的工作目录，不是守护进程建的。"""
    trash = Path(node.config["workspace_root"]) / workspace.TRASH_DIR
    trash.mkdir()
    trash.chmod(0o755)
    return trash


def test_an_ordinary_daemon_removes_only_entries_named_like_removed_workspaces(node):
    trash = ordinary_trash(node)
    (trash / ENTRY / "deep").mkdir(parents=True)
    (trash / ENTRY / "deep" / "f").write_text("x")
    (trash / "project").mkdir()
    (trash / "project" / "a.py").write_text("x")
    (trash / "notes.md").write_text("x")
    (trash / ("g" + ENTRY[1:])).mkdir()
    (trash / (ENTRY + "0")).mkdir()
    node.clear_assistant_leftovers()
    assert sorted(p.name for p in trash.iterdir()) == sorted(
        ["project", "notes.md", "g" + ENTRY[1:], ENTRY + "0"]
    )
    assert (trash / "project" / "a.py").read_text() == "x"
    assert trash.stat().st_mode & 0o777 == 0o755


def test_an_ordinary_daemon_leaves_a_trash_that_is_a_working_directory(node):
    trash = ordinary_trash(node)
    (trash / workspace.MARKER).write_text("{}")
    (trash / ENTRY).mkdir()
    node.clear_assistant_leftovers()
    assert (trash / ENTRY).is_dir() and (trash / workspace.MARKER).is_file()
    assert trash.stat().st_mode & 0o777 == 0o755


def test_an_ordinary_daemon_does_not_create_the_trash(node):
    node.clear_assistant_leftovers()
    assert not os.path.lexists(Path(node.config["workspace_root"]) / workspace.TRASH_DIR)

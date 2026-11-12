"""守护进程收拾助理留下的东西：保留目录不给助理用、回收站清扫、改回普通实例后的本轮目录。"""

from __future__ import annotations

import base64
import json
import tempfile
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


SANDBOXED = {"sandbox": True, "transcript": {"transfer": "0" * 32, "restore": False}}


@pytest.mark.parametrize(
    "directory", [".coreman-health", ".coreman-health/sub", ".coreman-trash/x"]
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

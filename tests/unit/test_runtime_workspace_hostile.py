"""助理写得到的工作目录：守护进程在沙箱外读写它时不跟随链接、不在 FIFO 上卡住、不越界。"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from runtime_daemon import workspace
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
    return agent.dispatch({"type": "workspace-" + kind, "working_dir": "one", "bot_id": "b1", **kw})


def finishes(fn, seconds: float = 5.0):
    """在线程里跑 `fn`：卡住（例如在 FIFO 上等写端）就判失败，而不是把整个测试进程挂住。"""
    out: dict = {}

    def run() -> None:
        try:
            out["value"] = fn()
        except Exception as exc:  # noqa: BLE001 交给断言
            out["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "blocked"
    if "error" in out:
        raise out["error"]
    return out.get("value")


def test_read_small_refuses_fifos_links_devices_and_large_files(tmp_path):
    regular = tmp_path / "ok.json"
    regular.write_bytes(b'{"bot_id": "b1"}')
    assert workspace.read_small(regular) == b'{"bot_id": "b1"}'
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert finishes(lambda: workspace.read_small(fifo)) is None
    link = tmp_path / "link"
    link.symlink_to(regular)
    assert workspace.read_small(link) is None
    assert workspace.read_small(Path("/dev/zero")) is None
    big = tmp_path / "big"
    big.write_bytes(b"x" * (workspace.MARKER_LIMIT + 1))
    assert workspace.read_small(big) is None
    assert workspace.read_small(tmp_path / "missing") is None


def test_a_fifo_marker_is_not_owned_and_does_not_block(agent):
    p = agent.root / "one"
    p.mkdir()
    os.mkfifo(p / workspace.MARKER)
    info = finishes(lambda: call(agent, "info"))
    assert info["owned"] is False and info["marked"] is True
    with pytest.raises(OperationError):
        finishes(lambda: call(agent, "init", content="x"))


@pytest.mark.parametrize("payload", [b"[]", b'"b1"', b"\xff\xfe", b'{"bot_id": "b2"}'])
def test_malformed_or_foreign_markers_are_not_owned(agent, payload):
    p = agent.root / "one"
    p.mkdir()
    (p / workspace.MARKER).write_bytes(payload)
    assert call(agent, "info")["owned"] is False


def test_assistant_only_init_never_reads_an_existing_directory(agent):
    agent.assistant_only = True
    first = call(agent, "init", content="# rules\n")
    assert first["initialized"] and call(agent, "info")["owned"]
    p = agent.root / "one"
    # 初始化做完之后助理在沙箱里改了说明文件：再初始化不读它（读目录会报错），原样返回。
    (p / "CLAUDE.md").unlink()
    (p / "CLAUDE.md").mkdir()
    again = call(agent, "init", content="# rules\n")
    assert again == {"success": True, "initialized": True, "owned": True, "workspace_protocol": 1}
    assert (p / "CLAUDE.md").is_dir()


def test_assistant_only_init_refuses_a_non_empty_unmarked_directory(agent):
    agent.assistant_only = True
    p = agent.root / "one"
    p.mkdir()
    (p / "notes.md").write_text("left behind")
    with pytest.raises(OperationError, match="全新的目录"):
        call(agent, "init", content="x")
    (p / "notes.md").unlink()
    assert call(agent, "init", content="x")["initialized"]


def test_ordinary_init_keeps_taking_over_existing_directories(agent):
    p = agent.root / "one"
    p.mkdir()
    (p / "notes.md").write_text("kept")
    assert call(agent, "init", content="x")["initialized"]
    assert (p / "notes.md").read_text() == "kept"

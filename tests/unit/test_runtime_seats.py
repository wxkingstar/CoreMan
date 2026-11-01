"""席位：同一节点上同一种 AI 的多个登录账号，配置目录分开、会话共用，切换时换驱动。

CLI 用假脚本：Claude 打印授权链接后从终端读认证码，Codex 打印设备码后等浏览器那边完成。
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

import runtime_daemon.daemon as module
from runtime_daemon.agent import OperationError
from runtime_daemon.seats import DEFAULT, SeatLogins, Seats
from tests.unit.runtime_daemon_support import make_daemon


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # 席位写的是进程环境；monkeypatch 记下原值，用例结束还原。
    for key in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        monkeypatch.delenv(key, raising=False)


def make_seats(tmp_path: Path) -> tuple[Seats, dict]:
    config: dict = {}
    return Seats(tmp_path / "data", config, lambda: None, home=tmp_path / "home"), config


def test_new_seat_has_its_own_config_dir_but_shares_conversations(tmp_path):
    seats, config = make_seats(tmp_path)
    default = tmp_path / "home/.claude"
    default.mkdir(parents=True)
    (default / "settings.json").write_text("{}")
    seat = seats.create("claude", " 张三 ")
    directory = seats.directory("claude", seat)
    assert directory is not None and directory.is_dir()
    assert config["seats"]["claude"]["items"][seat] == {"label": "张三"}
    assert (directory / "projects").resolve() == (default / "projects").resolve()
    assert (directory / "settings.json").resolve() == (default / "settings.json").resolve()
    # 没有的文件不建链接，凭据与账号状态各席位自己的。
    assert not (directory / "CLAUDE.md").exists() and not (directory / ".credentials.json").exists()
    assert seats.env("claude", seat)["CLAUDE_CONFIG_DIR"] == str(directory)
    assert "CLAUDE_CONFIG_DIR" not in seats.env("claude", DEFAULT)


def test_activate_writes_the_seat_into_the_process_environment(tmp_path):
    seats, _ = make_seats(tmp_path)
    seat = seats.create("codex", "B")
    seats.activate("codex", seat)
    assert os.environ["CODEX_HOME"] == str(seats.directory("codex", seat))
    assert (Path(os.environ["CODEX_HOME"]) / "sessions").resolve() == (
        tmp_path / "home/.codex/sessions"
    ).resolve()
    seats.activate("codex", DEFAULT)
    assert "CODEX_HOME" not in os.environ


def test_default_seat_keeps_a_directory_given_by_the_service(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "custom"))
    seats, _ = make_seats(tmp_path)
    seat = seats.create("claude", "B")
    assert (seats.directory("claude", seat) / "projects").resolve() == (
        tmp_path / "custom/projects"
    ).resolve()
    seats.activate("claude", seat)
    seats.activate("claude", DEFAULT)
    assert os.environ["CLAUDE_CONFIG_DIR"] == str(tmp_path / "custom")


def test_remove_keeps_shared_content_and_refuses_default_or_active(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")  # 没有 CLI 时退出登录失败也照样删
    seats, config = make_seats(tmp_path)
    seat = seats.create("claude", "B")
    shared = tmp_path / "home/.claude/projects/keep.jsonl"
    shared.write_text("x")
    with pytest.raises(OperationError, match="默认席位"):
        seats.remove("claude", DEFAULT)
    seats.activate("claude", seat)
    with pytest.raises(OperationError, match="当前席位"):
        seats.remove("claude", seat)
    seats.activate("claude", DEFAULT)
    seats.remove("claude", seat)
    assert not seats.directory("claude", seat).exists()
    assert shared.read_text() == "x" and config["seats"]["claude"]["items"] == {}


def test_unknown_or_malformed_seat_ids_are_rejected(tmp_path):
    seats, _ = make_seats(tmp_path)
    for value in ("../x", "abc", None, "0" * 12):
        with pytest.raises(OperationError):
            seats.require("claude", value)
    with pytest.raises(OperationError, match="名称"):
        seats.create("claude", "  ")


FAKE_CLI = textwrap.dedent(
    """\
    #!{python}
    import os, sys, time
    args = sys.argv[1:]
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("CODEX_HOME") or ""
    if os.path.basename(sys.argv[0]) == "claude" and args[:2] == ["auth", "login"]:
        print("Opening browser to sign in...", flush=True)
        print("If the browser didn't open, visit: https://claude.com/cai/oauth/authorize"
              "?code=true&state=abc", flush=True)
        print("Paste code here if prompted > ", end="", flush=True)
        code = sys.stdin.readline().strip()
        if code != "good-code-123#state":
            print("Invalid code", flush=True)
            sys.exit(1)
        open(os.path.join(home, ".credentials.json"), "w").write("{{}}")
        print("Login successful", flush=True)
        sys.exit(0)
    if os.path.basename(sys.argv[0]) == "codex" and args[:2] == ["login", "--device-auth"]:
        print("1. Open this link in your browser and sign in to your account", flush=True)
        print("   \\x1b[94mhttps://auth.openai.com/codex/device\\x1b[0m", flush=True)
        print("2. Enter this one-time code", flush=True)
        print("   \\x1b[94mT4RE-EK8H2\\x1b[0m", flush=True)
        done = os.path.join(home, "browser-done")
        while not os.path.exists(done):
            time.sleep(0.1)
        open(os.path.join(home, "auth.json"), "w").write("{{}}")
        sys.exit(0)
    sys.exit(0)
    """
)


@pytest.fixture
def cli(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("codex", "claude"):
        script = bin_dir / name
        script.write_text(FAKE_CLI.format(python=sys.executable))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def test_claude_login_hands_out_the_link_and_feeds_the_code_back(tmp_path, cli):
    seats, _ = make_seats(tmp_path)
    seat = seats.create("claude", "B")
    logins = SeatLogins()
    started = logins.start("claude", seat, seats.env("claude", seat), tmp_path)
    assert started["authorization_url"] == (
        "https://claude.com/cai/oauth/authorize?code=true&state=abc"
    )
    assert started["needs_code"] is True and started["user_code"] == ""
    with pytest.raises(OperationError, match="格式"):
        logins.complete(started["session_id"], "claude", seat, "bad code with spaces")
    with pytest.raises(OperationError, match="过期"):
        logins.complete(started["session_id"], "claude", DEFAULT, "good-code-123#state")
    result = logins.complete(started["session_id"], "claude", seat, " good-code-123#state\n")
    assert result["success"] is True
    assert (seats.directory("claude", seat) / ".credentials.json").exists()
    with pytest.raises(OperationError, match="过期"):
        logins.complete(started["session_id"], "claude", seat, "good-code-123#state")


def test_claude_login_with_a_wrong_code_fails(tmp_path, cli):
    seats, _ = make_seats(tmp_path)
    seat = seats.create("claude", "B")
    logins = SeatLogins()
    started = logins.start("claude", seat, seats.env("claude", seat), tmp_path)
    with pytest.raises(OperationError, match="未能完成"):
        logins.complete(started["session_id"], "claude", seat, "wrong-code-999")


def test_codex_device_login_waits_for_the_browser(tmp_path, cli):
    seats, _ = make_seats(tmp_path)
    seat = seats.create("codex", "B")
    logins = SeatLogins()
    started = logins.start("codex", seat, seats.env("codex", seat), tmp_path)
    assert started["authorization_url"] == "https://auth.openai.com/codex/device"
    assert started["user_code"] == "T4RE-EK8H2" and started["needs_code"] is False
    with pytest.raises(OperationError, match="还没有"):
        logins.complete(started["session_id"], "codex", seat, None)
    (seats.directory("codex", seat) / "browser-done").write_text("")
    assert logins.complete(started["session_id"], "codex", seat, None)["success"] is True
    assert (seats.directory("codex", seat) / "auth.json").exists()


def sleeping_process() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    instance = make_daemon(tmp_path)
    instance.socket_dir = Path(tempfile.mkdtemp(prefix="cm-", dir="/tmp"))
    started: list[str] = []

    def fake_start(provider):
        started.append(provider)
        instance.drivers[provider] = sleeping_process()

    monkeypatch.setattr(instance, "_start_driver", fake_start)
    instance.started = started
    yield instance
    for process in [*instance.drivers.values(), *(p for _, _, p in instance.retired)]:
        if process.poll() is None:
            process.kill()
            process.wait()
    shutil.rmtree(instance.socket_dir, ignore_errors=True)


def test_rotation_keeps_the_old_driver_until_its_requests_finish(daemon):
    old = sleeping_process()
    daemon.drivers["claude"] = old
    daemon.task_info["chat-1"] = {"provider": "claude", "driver_gen": 0}
    # 节点操作（技能安装等）不经驱动，不拖住旧驱动。
    daemon.task_info["op-1"] = {"provider": "claude"}
    daemon.rotate_driver("claude")
    assert daemon.driver_gen["claude"] == 1 and daemon.started == ["claude"]
    assert daemon.drivers["claude"] is not old and old.poll() is None
    assert daemon.socket_path("claude").name == "claude-1.sock"
    assert daemon.socket_path("claude", 0).name == "claude.sock"
    daemon.task_info.pop("chat-1")
    daemon.reap_retired()
    assert daemon.retired == []
    old.wait(timeout=10)


def test_activating_a_seat_switches_the_driver_and_reports_seats(daemon, monkeypatch):
    statuses = {}

    def fake_status(provider, env=None):
        home = (env or {}).get("CLAUDE_CONFIG_DIR", "default")
        return {"installed": True, "login": statuses.get(home, "ready"), "account": home}

    monkeypatch.setattr(module, "cli_status", fake_status)
    daemon.capabilities["claude"] = {
        "installed": True,
        "login": "ready",
        "account": "a@example.com",
    }
    agent = module.RuntimeAgent(daemon, "claude", "relay-claude")
    created = agent.dispatch({"type": "seat-create", "label": "张三"})
    seat = created["seat_id"]
    assert [s["id"] for s in created["seats"]] == [DEFAULT, seat]
    assert created["seats"][0] == {
        "id": DEFAULT,
        "label": "",
        "account": "a@example.com",
        "login": "ready",
        "active": True,
    }
    statuses[str(daemon.seats.directory("claude", seat))] = "required"
    with pytest.raises(OperationError, match="未登录"):
        agent.dispatch({"type": "seat-activate", "seat_id": seat})
    statuses.clear()
    switched = agent.dispatch({"type": "seat-activate", "seat_id": seat})
    assert [s["active"] for s in switched["seats"]] == [False, True]
    assert os.environ["CLAUDE_CONFIG_DIR"] == str(daemon.seats.directory("claude", seat))
    assert daemon.started == ["claude"] and daemon.driver_gen["claude"] == 1
    saved = json.loads(daemon.config_path.read_text())
    assert saved["seats"]["claude"]["active"] == seat
    # 已是当前席位：不再换驱动。
    agent.dispatch({"type": "seat-activate", "seat_id": seat})
    assert daemon.driver_gen["claude"] == 1
    with pytest.raises(OperationError, match="当前席位"):
        agent.dispatch({"type": "seat-remove", "seat_id": seat})
    agent.stop.set()

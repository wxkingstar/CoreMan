"""Daemon behaviour behind the deployment fixes: TLS wiring, socket self-healing, fatal errors."""

import json
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
import pytest

import runtime_daemon.daemon as module
from runtime_daemon.daemon import HEAL_DEFER_ROUNDS, SOCKET_FAILURE_LIMIT, RuntimeAgent
from runtime_daemon.lifecycle import EXIT_CONFIG, FatalConfigError
from tests.unit.runtime_daemon_support import make_daemon, make_pki

FAKE_DRIVER = """
import argparse, http.server, json, socketserver
parser = argparse.ArgumentParser()
for flag in ("--socket", "--parent-pid", "--sessions-dir", "--log-file"):
    parser.add_argument(flag)
args = parser.parse_args()

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"data": [{"id": "fake-model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        print("request", flush=True)

print("listening", flush=True)
socketserver.UnixStreamServer(args.socket, Handler).serve_forever()
"""


def fake_status(provider):
    return {
        "installed": provider == "claude",
        "version": "test",
        "login": "ready",
        "health": "unknown",
        "models": [],
        "detail": "",
    }


@pytest.fixture
def healing_daemon(tmp_path, monkeypatch):
    release = tmp_path / "release"
    driver = release / "runtime_daemon/bin/runtime-claude"
    driver.parent.mkdir(parents=True)
    driver.write_text("#!" + sys.executable + "\n" + FAKE_DRIVER)
    driver.chmod(0o700)
    daemon = make_daemon(tmp_path, release=str(release))
    # pytest paths exceed the sockaddr limit on macOS.
    daemon.socket_dir = Path(tempfile.mkdtemp(prefix="cm-", dir="/tmp"))
    monkeypatch.setattr(module, "cli_status", fake_status)
    yield daemon
    for process in daemon.drivers.values():
        if process.poll() is None:
            process.kill()
            process.wait()
    for agent in daemon.agents.values():
        agent.stop.set()
    shutil.rmtree(daemon.socket_dir, ignore_errors=True)


async def test_discovery_restarts_a_driver_whose_socket_directory_was_cleaned(healing_daemon):
    daemon = healing_daemon
    await daemon.discover()
    assert daemon.capabilities["claude"]["models"] == ["fake-model"]
    first = daemon.drivers["claude"]
    # tmpfiles/tmpwatch remove idle entries while the driver process keeps running.
    shutil.rmtree(daemon.socket_dir)
    assert daemon.driver_fault("claude") == "socket missing"
    await daemon.discover()
    second = daemon.drivers["claude"]
    assert second is not first and first.poll() is not None and second.poll() is None
    assert daemon.capabilities["claude"]["models"] == ["fake-model"]
    assert daemon.socket_path("claude").is_socket()
    assert daemon.socket_dir.stat().st_mode & 0o777 == 0o700
    log = daemon.data_dir / "claude.log"
    assert log.stat().st_mode & 0o777 == 0o600
    assert log in daemon.log_rotator.targets


def sleeping_process() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


async def test_restart_waits_for_running_requests_but_not_forever(healing_daemon):
    daemon = healing_daemon
    process = sleeping_process()
    daemon.drivers["claude"] = process
    daemon.task_info["call-1"] = {"provider": "claude"}
    await daemon.heal_driver("claude")
    assert process.poll() is None and daemon.heal_deferrals["claude"] == 1
    daemon.heal_deferrals["claude"] = HEAL_DEFER_ROUNDS
    await daemon.heal_driver("claude")
    assert process.poll() is not None


def test_refused_connections_count_toward_a_restart(healing_daemon):
    daemon = healing_daemon
    daemon.drivers["claude"] = process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(daemon.socket_path("claude")))
    stale.close()  # the file stays, nothing listens
    assert daemon.driver_fault("claude") == ""
    daemon.socket_failures["claude"] = SOCKET_FAILURE_LIMIT
    assert daemon.driver_fault("claude") == "socket unreachable"
    process.kill()
    process.wait()
    assert daemon.driver_fault("claude") == ""


async def test_connect_errors_increment_the_failure_count(healing_daemon, monkeypatch):
    daemon = healing_daemon
    monkeypatch.setattr(daemon, "start_driver", lambda provider: None)
    for expected in (1, 2):
        await daemon.discover()
        assert daemon.socket_failures["claude"] == expected
    assert daemon.capabilities["claude"]["detail"] == "AI API 启动中或不可用"


def test_runtime_agent_shares_the_daemon_trust_chain(tmp_path):
    daemon = make_daemon(tmp_path)
    daemon.trust_file = tmp_path / "trust.pem"
    agent = RuntimeAgent(daemon, "claude", "relay-claude")
    assert agent.http.verify == str(daemon.trust_file)
    assert agent.http.trust_env is False
    daemon.trust_file = None
    assert RuntimeAgent(daemon, "codex", "relay-codex").http.verify is True


def test_prepare_builds_trust_chain_and_private_socket_dir(tmp_path, monkeypatch):
    pki = make_pki(tmp_path / "pki")
    daemon = make_daemon(tmp_path, ca_file=str(pki.ca))
    socket_dir = Path(tempfile.mkdtemp(prefix="cm-", dir="/tmp")) / "run"
    monkeypatch.setattr(module, "choose_socket_dir", lambda data_dir, node_id: socket_dir)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    try:
        daemon.prepare()
        assert daemon.socket_dir == socket_dir
        assert socket_dir.stat().st_mode & 0o777 == 0o700
        assert daemon.trust_file == tmp_path / "trust.pem"
        assert pki.ca.read_bytes().strip() in daemon.trust_file.read_bytes()
        assert isinstance(daemon.tls_context, ssl.SSLContext)
        assert daemon.log_rotator.thread is not None
    finally:
        daemon.log_rotator.stop()
        daemon.lock_file.close()
        shutil.rmtree(socket_dir.parent, ignore_errors=True)


def enroll_daemon(tmp_path, status, body):
    daemon = make_daemon(tmp_path, install_token="link.token")
    daemon.config.pop("backends")
    daemon.http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body)),
        base_url="http://coreman.test",
    )
    return daemon


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 410, 422])
async def test_rejected_enrollment_is_fatal(tmp_path, status):
    daemon = enroll_daemon(tmp_path, status, {"code": status, "message": "安装链接已过期"})
    async with daemon.http:
        with pytest.raises(FatalConfigError, match="安装链接已过期") as caught:
            await daemon.enroll()
    assert f"HTTP {status}" in str(caught.value)
    assert "install_token" in daemon.config and "backends" not in daemon.config


@pytest.mark.parametrize("status", [408, 500])
async def test_transient_enrollment_errors_still_restart(tmp_path, status):
    daemon = enroll_daemon(tmp_path, status, {"code": status, "message": "busy"})
    async with daemon.http:
        with pytest.raises(httpx.HTTPStatusError):
            await daemon.enroll()


@pytest.mark.parametrize(
    "service_status,code",
    [("systemd-user", EXIT_CONFIG), ("supervised", EXIT_CONFIG), ("launchd", 0)],
)
def test_main_records_fatal_state_and_stops_the_restart_loop(
    tmp_path, monkeypatch, service_status, code
):
    daemon = make_daemon(tmp_path, service_status=service_status)

    async def rejected(self):
        raise FatalConfigError("安装注册被 CoreMan 拒绝（HTTP 410：安装链接已过期）")

    monkeypatch.setattr(module.Daemon, "run", rejected)
    monkeypatch.setattr(module, "configure_logging", lambda *args, **kwargs: [])
    monkeypatch.setattr(sys, "argv", ["daemon", "--config", str(daemon.config_path)])
    with pytest.raises(SystemExit) as caught:
        module.main()
    assert caught.value.code == code
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["fatal"] is True and state["online"] is False
    assert "安装链接已过期" in state["error"]


def test_main_exits_nonzero_on_unexpected_errors(tmp_path, monkeypatch):
    daemon = make_daemon(tmp_path)

    async def crash(self):
        raise RuntimeError("boom")

    monkeypatch.setattr(module.Daemon, "run", crash)
    monkeypatch.setattr(module, "configure_logging", lambda *args, **kwargs: [])
    monkeypatch.setattr(sys, "argv", ["daemon", "--config", str(daemon.config_path)])
    with pytest.raises(SystemExit) as caught:
        module.main()
    assert caught.value.code == 1
    assert not (tmp_path / "state.json").exists()


@pytest.mark.parametrize("advertised", [None, False, "true", True])
async def test_personal_capability_comes_only_from_driver_health(healing_daemon, advertised):
    daemon = healing_daemon
    driver = Path(daemon.config["release"]) / "runtime_daemon/bin/runtime-claude"
    source = driver.read_text()
    source = source.replace(
        '{"data": [{"id": "fake-model"}]}',
        '{"data": [{"id": "fake-model"}], "capabilities": {"feishu_personal_restricted_v1": '
        + repr(advertised)
        + "}}",
    )
    driver.write_text(source)
    await daemon.discover()
    assert daemon.capabilities["claude"]["feishu_personal_restricted_v1"] is (advertised is True)

"""MCP OAuth 登录：节点用 CLI 的 --no-browser 登录，回调地址经平台送回 CLI 的回环端口。

用假的 codex / claude 脚本模拟两个 CLI：打印授权链接、在 127.0.0.1 上等回调，收到授权码后退出。
"""

import json
import os
import stat
import sys
import textwrap
import threading
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from runtime_daemon import agent as agent_module
from runtime_daemon import mcp_oauth

FAKE_CLI = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys, threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlsplit

    args = sys.argv[1:]
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(json.dumps({{"cli": os.path.basename(sys.argv[0]), "args": args,
                               "tty": os.isatty(0), "cwd": os.getcwd()}}) + "\\n")
    if args[:2] == ["mcp", "logout"]:
        needs_file = os.environ.get("FAKE_LOGOUT_NEEDS_FILE")
        sys.exit(1 if needs_file and "file" not in " ".join(args) else 0)
    if args[:2] != ["mcp", "login"]:
        sys.exit(0)
    result = {{}}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            result.update(parse_qs(urlsplit(self.path).query))
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
            done.set()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    host = os.environ.get("FAKE_REDIRECT_HOST", "127.0.0.1")
    if os.environ.get("FAKE_NO_URL"):
        print("this server does not support OAuth", flush=True)
        sys.exit(1)
    print("Authorize by opening:", flush=True)
    print("\\x1b]8;;https://auth.example.com/oauth?client_id=c&state=STATE-1&redirect_uri="
          "http%3A%2F%2F" + host + "%3A" + str(port) + "%2Fcallback\\x1b\\\\link\\x1b]8;;\\x1b\\\\",
          flush=True)
    print("https://auth.example.com/oauth?client_id=c&state=STATE-1&redirect_uri="
          "http%3A%2F%2F" + host + "%3A" + str(port) + "%2Fcallback", flush=True)
    done.wait(20)
    sys.exit(0 if result.get("code") == ["good-code"] else 2)
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
    log = tmp_path / "cli.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    return log


def calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture
def agent(tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    return agent_module.Agent(
        root=root,
        home=tmp_path / "home",
        api_url="https://coreman.example",
        token="synthetic-test-token-123456789",
        relay_id="test-relay",
        provider="codex",
    )


def project(agent, servers):
    path = agent.root / "bot"
    path.mkdir(exist_ok=True)
    (path / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
    return path


REMOTE = {"type": "http", "url": "https://mcp.example.com/mcp"}


def start(agent, path, name="figma-1a2b3c4d"):
    return agent.dispatch(
        {"type": "mcp-login-start", "project_dir": str(path), "server_name": name}
    )


def callback(result, **query):
    redirect = parse_qs(urlsplit(result["authorization_url"]).query)["redirect_uri"][0]
    params = {"code": "good-code", "state": "STATE-1", **query}
    return redirect + "?" + "&".join(f"{k}={v}" for k, v in params.items() if v is not None)


def test_codex_login_hands_the_callback_to_the_cli(agent, cli):
    path = project(agent, {"figma-1a2b3c4d": REMOTE})
    result = start(agent, path)
    assert result["authorization_url"].startswith("https://auth.example.com/oauth?")
    assert "\x1b" not in result["authorization_url"]
    login = calls(cli)[0]
    assert login["tty"] and login["cwd"] == str(path)
    # Codex 驱动以 skill_<名字> 挂载，登录必须用同一个名字和地址，令牌才对得上。
    assert login["args"] == [
        "mcp",
        "login",
        "skill_figma-1a2b3c4d",
        "--no-browser",
        "-c",
        'mcp_servers.skill_figma-1a2b3c4d.url="https://mcp.example.com/mcp"',
    ]
    done = agent.dispatch(
        {
            "type": "mcp-login-complete",
            "project_dir": str(path),
            "server_name": "figma-1a2b3c4d",
            "session_id": result["session_id"],
            "callback_url": " " + callback(result) + " ",
        }
    )
    assert done == {"success": True, "message": "授权完成"}
    with pytest.raises(agent_module.OperationError, match="过期"):
        agent.dispatch(
            {
                "type": "mcp-login-complete",
                "project_dir": str(path),
                "server_name": "figma-1a2b3c4d",
                "session_id": result["session_id"],
                "callback_url": callback(result),
            }
        )


def test_claude_login_registers_the_server_in_local_scope(agent, cli):
    agent.provider = "claude"
    path = project(agent, {"figma-1a2b3c4d": REMOTE})
    result = start(agent, path)
    commands = [c["args"] for c in calls(cli)]
    assert commands[:3] == [
        ["mcp", "remove", "figma-1a2b3c4d", "-s", "local"],
        ["mcp", "add-json", "-s", "local", "figma-1a2b3c4d", json.dumps(REMOTE)],
        ["mcp", "login", "figma-1a2b3c4d", "--no-browser"],
    ]
    assert calls(cli)[2]["tty"]
    assert agent.dispatch(
        {
            "type": "mcp-login-complete",
            "project_dir": str(path),
            "server_name": "figma-1a2b3c4d",
            "session_id": result["session_id"],
            "callback_url": callback(result),
        }
    )["success"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda url: url.replace("127.0.0.1", "203.0.113.5"), "不是本次授权"),
        (lambda url: url.replace("/callback", "/other"), "不是本次授权"),
        (lambda url: url.replace("http://", "https://"), "不是本次授权"),
        (lambda url: url.replace("STATE-1", "STATE-2"), "不匹配"),
        (lambda url: url.replace("code=good-code", "error=access_denied"), "拒绝"),
    ],
)
def test_complete_only_accepts_this_sessions_loopback_callback(agent, cli, mutate, message):
    path = project(agent, {"figma-1a2b3c4d": REMOTE})
    result = start(agent, path)
    with pytest.raises(agent_module.OperationError, match=message):
        agent.dispatch(
            {
                "type": "mcp-login-complete",
                "project_dir": str(path),
                "server_name": "figma-1a2b3c4d",
                "session_id": result["session_id"],
                "callback_url": mutate(callback(result)),
            }
        )
    agent.mcp_logins.close(result["session_id"])


def test_complete_is_bound_to_the_same_employee(agent, cli):
    path = project(agent, {"figma-1a2b3c4d": REMOTE, "figma-99999999": REMOTE})
    result = start(agent, path)
    with pytest.raises(agent_module.OperationError, match="过期"):
        agent.dispatch(
            {
                "type": "mcp-login-complete",
                "project_dir": str(path),
                "server_name": "figma-99999999",
                "session_id": result["session_id"],
                "callback_url": callback(result),
            }
        )
    agent.mcp_logins.close(result["session_id"])


@pytest.mark.parametrize(
    "servers",
    [
        {},
        {"figma-1a2b3c4d": {"command": "npx"}},
        {"figma-1a2b3c4d": {"type": "http", "url": "http://mcp.example.com/mcp"}},
    ],
)
def test_start_needs_an_installed_remote_server(agent, cli, servers):
    path = project(agent, servers)
    with pytest.raises(agent_module.OperationError):
        start(agent, path)
    assert calls(cli) == []


def test_start_reports_servers_without_oauth(agent, cli, monkeypatch):
    monkeypatch.setenv("FAKE_NO_URL", "1")
    path = project(agent, {"figma-1a2b3c4d": REMOTE})
    with pytest.raises(agent_module.OperationError, match="不支持 OAuth"):
        start(agent, path)
    assert agent.mcp_logins.sessions == {}


def test_start_refuses_a_redirect_off_the_node(agent, cli, monkeypatch):
    monkeypatch.setenv("FAKE_REDIRECT_HOST", "attacker.example")
    path = project(agent, {"figma-1a2b3c4d": REMOTE})
    with pytest.raises(agent_module.OperationError, match="本机"):
        start(agent, path)
    assert agent.mcp_logins.sessions == {}


def test_logout_falls_back_to_the_file_store(agent, cli, monkeypatch):
    monkeypatch.setenv("FAKE_LOGOUT_NEEDS_FILE", "1")
    path = project(agent, {"figma-1a2b3c4d": REMOTE})
    result = agent.dispatch(
        {"type": "mcp-logout", "project_dir": str(path), "server_name": "figma-1a2b3c4d"}
    )
    assert result["success"]
    first, second = (c["args"] for c in calls(cli))
    assert first[:3] == ["mcp", "logout", "skill_figma-1a2b3c4d"]
    assert 'mcp_oauth_credentials_store="file"' not in first
    assert 'mcp_oauth_credentials_store="file"' in second


def test_login_does_not_hold_the_instance_lock(agent, cli):
    path = project(agent, {"figma-1a2b3c4d": REMOTE})
    with agent.lock:
        holder = {}
        thread = threading.Thread(target=lambda: holder.update(start(agent, path)))
        thread.start()
        thread.join(15)
    assert holder["success"]
    agent.mcp_logins.close(holder["session_id"])


def test_expired_sessions_are_stopped(agent, cli, monkeypatch):
    path = project(agent, {"figma-1a2b3c4d": REMOTE})
    result = start(agent, path)
    session = agent.mcp_logins.sessions[result["session_id"]]
    monkeypatch.setattr(mcp_oauth, "SESSION_TTL", -1)
    agent.mcp_logins.reap()
    assert agent.mcp_logins.sessions == {}
    assert session.proc.wait(timeout=5) is not None
    with pytest.raises(requests.RequestException):
        requests.get(callback(result), timeout=2)


def test_install_names_oauth_servers_per_employee(agent):
    bot = agent.root / "bot"
    bot.mkdir()
    (bot / ".mcp.json").write_text(json.dumps({"mcpServers": {"figma": REMOTE, "other": {}}}))
    agent.install_skill(
        {
            "project_dir": str(bot),
            "install_type": "mcp",
            "skill_name": "figma",
            "server_name": "figma-1a2b3c4d",
            "mcp_config": REMOTE,
        }
    )
    assert json.loads((bot / ".mcp.json").read_text())["mcpServers"] == {
        "other": {},
        "figma-1a2b3c4d": REMOTE,
    }
    with pytest.raises(agent_module.OperationError):
        agent.install_skill(
            {
                "project_dir": str(bot),
                "install_type": "mcp",
                "skill_name": "figma",
                "server_name": "../escape",
                "mcp_config": REMOTE,
            }
        )


def test_ping_announces_mcp_oauth(agent):
    assert agent.dispatch({"type": "ping"})["mcp_oauth"] == 1


def test_abandoned_sessions_end_on_their_own(agent, cli, monkeypatch):
    monkeypatch.setattr(mcp_oauth, "SESSION_TTL", 0.5)
    path = project(agent, {"figma-1a2b3c4d": REMOTE})
    result = start(agent, path)
    session = agent.mcp_logins.sessions[result["session_id"]]
    assert session.proc.wait(timeout=5) is not None
    assert agent.mcp_logins.sessions == {}

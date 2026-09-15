import json
import subprocess
import threading

import pytest

from runtime_daemon import agent as agent_module


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
    )


def test_workspace_and_git_sources_fail_closed(agent, tmp_path):
    assert agent.workspace("test") == agent.root / "test"
    for path in (str(tmp_path), "../escape", str(agent.root)):
        with pytest.raises(agent_module.OperationError):
            agent.workspace(path)
    (agent.root / "link").symlink_to(tmp_path)
    with pytest.raises(agent_module.OperationError):
        agent.workspace("link")
    assert agent.git_source("git@github.com:example/skills.git@my-skill") == (
        "git@github.com:example/skills.git",
        "my-skill",
    )
    for url in (
        "file:///tmp/repo",
        "https://evil.example/repo",
        "https://user:secret@github.com/repo",
        "--upload-pack=evil",
    ):
        with pytest.raises(agent_module.OperationError):
            agent.git_source(url)


def test_memory_batch_validates_before_writes_and_preserves_file_contents(agent):
    bot = agent.root / "bot"
    bot.mkdir()
    with pytest.raises(agent_module.OperationError):
        agent.deploy_memory(
            [
                {"working_dir": str(bot), "file_name": "ok.md", "content": "ok"},
                {"working_dir": str(bot), "file_name": "../bad.md", "content": "bad"},
            ]
        )
    assert not (agent.memory_dir(str(bot)) / "ok.md").exists()
    assert (
        agent.deploy_memory([{"working_dir": str(bot), "file_name": "ok.md", "content": "Content"}])
        == 1
    )
    collected = agent.collect_memory(str(bot))
    assert len(collected) == 1 and collected[0]["content"] == "Content"
    assert collected[0]["working_dir"] == str(bot)
    assert len(collected[0]["content_hash"]) == 64


def test_pull_refuses_dirty_repo_without_reset_or_clean(agent, monkeypatch):
    repo = agent.root / "bot"
    (repo / ".git").mkdir(parents=True)
    calls = []

    def run(command, *args, **kwargs):
        calls.append(command)
        return "https://github.com/example/bot.git" if "remote" in command else " M work.py"

    monkeypatch.setattr(agent_module, "run_command", run)
    with pytest.raises(agent_module.OperationError):
        agent.pull({"git_url": "https://github.com/example/bot.git", "working_dir": str(repo)})
    assert len(calls) == 2
    assert all("reset" not in c and "clean" not in c for c in calls)


def test_initialize_repo_commits_explicit_docs_even_when_locally_excluded(agent, monkeypatch):
    repo = agent.root / "bot"
    (repo / ".git/info").mkdir(parents=True)
    agent.prepare_workspace(repo)
    commands = []
    monkeypatch.setattr(
        agent_module, "run_command", lambda command, *a, **k: commands.append(command) or ""
    )
    monkeypatch.setattr(agent, "pull", lambda data: {"working_dir": str(repo)})
    agent.initialize_repo(
        {"git_url": "https://github.com/example/bot.git", "content": "instructions"}
    )
    assert ["git", "add", "-f", "--", "CLAUDE.md", "AGENTS.md"] in commands
    assert (repo / "AGENTS.md").read_text() == "instructions"


def test_explicit_probe_install_preserves_existing_settings(agent):
    settings = agent.home / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"other": {"keep": True}}))
    agent.install_claude_probe()
    assert json.loads(settings.read_text())["other"] == {"keep": True}
    script = agent.home / ".cache/claude_rate_limits/probe.sh"
    assert "claude" in script.read_text()
    assert "dangerously-skip-permissions" not in script.read_text()
    settings.write_text("broken-json")
    with pytest.raises(ValueError):
        agent.install_claude_probe()
    assert settings.read_text() == "broken-json"


def run_capture(agent, payload: str) -> str:
    capture = agent.home / ".cache/claude_rate_limits/capture.sh"
    return subprocess.run(
        ["/bin/sh", str(capture)],
        input=payload,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout


def test_probe_install_keeps_the_existing_status_line_working(agent):
    settings = agent.home / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    original = {"type": "command", "command": "tr a-z A-Z", "padding": 0}
    settings.write_text(json.dumps({"statusLine": original, "theme": "dark"}))
    agent.install_claude_probe()
    cache = agent.home / ".cache/claude_rate_limits"
    status = json.loads(settings.read_text())["statusLine"]
    assert status == {"type": "command", "command": str(cache / "capture.sh"), "padding": 0}
    payload = '{"rate_limits":{"five_hour":{"used_percentage":2}}}'
    assert run_capture(agent, payload) == payload.upper()
    assert (cache / "rate_limits.json").read_text() == payload
    backup = json.loads(settings.with_name("settings.before-coreman-probe.json").read_text())
    assert backup["statusLine"] == original

    # 守护进程每次启动都会重装：不能把采集脚本包进自己，也不覆盖用户之后调整的字段。
    status["padding"] = 2
    settings.write_text(json.dumps({"statusLine": status, "theme": "dark"}))
    agent.install_claude_probe()
    agent.install_claude_probe()
    assert json.loads(settings.read_text()) == {"statusLine": status, "theme": "dark"}
    assert run_capture(agent, '{"model":"x"}') == '{"MODEL":"X"}'
    assert str(cache / "capture.sh") not in (cache / "capture.sh").read_text()


def test_probe_install_recovers_a_status_line_replaced_by_the_old_probe(agent):
    capture = agent.home / ".cache/claude_rate_limits/capture.sh"
    settings = agent.home / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.with_name("settings.before-coreman-probe.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": "printf legacy"}})
    )
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": str(capture)}}))
    agent.install_claude_probe()
    assert run_capture(agent, "{}") == "legacy"


def test_probe_install_without_a_status_line_and_with_an_unknown_one(agent):
    cache = agent.home / ".cache/claude_rate_limits"
    settings = agent.home / ".claude/settings.json"
    agent.install_claude_probe()
    assert json.loads(settings.read_text())["statusLine"]["command"] == str(cache / "capture.sh")
    assert run_capture(agent, '{"rate_limits":{}}') == ""
    # 空输入不能把上次采到的数据清空。
    assert run_capture(agent, "") == ""
    assert (cache / "rate_limits.json").read_text() == '{"rate_limits":{}}'
    settings.write_text(json.dumps({"statusLine": "bunx ccstatusline"}))
    with pytest.raises(agent_module.OperationError):
        agent.install_claude_probe()
    assert json.loads(settings.read_text()) == {"statusLine": "bunx ccstatusline"}


def run_schedule_once(agent, monkeypatch) -> list[str]:
    started: list[str] = []

    def record(name, function):
        started.append(name)
        agent.stop.set()
        return {"success": True}

    monkeypatch.setattr(agent, "start_background", record)
    monkeypatch.setenv("COREMAN_MEMORY_SYNC", "1")
    # 刚开机的主机 monotonic 很小（CI 虚拟机即如此），第一轮仍应在启动后立即排上。
    monkeypatch.setattr(agent_module.time, "monotonic", lambda: 5.0)
    agent.stop.clear()
    agent.start_schedules()
    for thread in threading.enumerate():
        if thread.name == "coreman-agent-schedules":
            thread.join(timeout=5)
    return started


def test_quota_probe_is_not_scheduled_until_the_claude_probe_is_enabled(agent, monkeypatch):
    assert "probe-rate-limits" not in run_schedule_once(agent, monkeypatch)
    result = agent.dispatch({"type": "probe-rate-limits"})
    assert result["success"] is False
    assert "install_claude_probe" in result["message"]
    assert "--install-claude-probe" not in result["message"]
    (agent.home / ".cache/claude_rate_limits").mkdir(parents=True)
    (agent.home / ".cache/claude_rate_limits/probe.sh").write_text("unused")
    assert "probe-rate-limits" in run_schedule_once(agent, monkeypatch)
    agent.provider = "codex"
    (agent.home / ".cache/claude_rate_limits/probe.sh").unlink()
    assert agent.rate_limit_probe_enabled()


def test_background_failure_log_keeps_operation_messages_only(agent, capsys):
    def raising(exc):
        def function():
            raise exc

        return function

    failures = (
        ("known", agent_module.OperationError("额度探测没有刷新数据")),
        ("unknown", ValueError("/home/someone/private-path")),
    )
    for name, exc in failures:
        agent.start_background(name, raising(exc))
        for thread in threading.enumerate():
            if thread.name == name:
                thread.join(timeout=5)
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events == [
        {
            "event": "agent_operation_failed",
            "operation": "known",
            "error": "OperationError",
            "message": "额度探测没有刷新数据",
        },
        {"event": "agent_operation_failed", "operation": "unknown", "error": "ValueError"},
    ]


def test_failed_probe_does_not_publish_stale_quota(agent, monkeypatch):
    cache = agent.home / ".cache/claude_rate_limits"
    cache.mkdir(parents=True)
    (cache / "probe.sh").write_text("unused")
    (cache / "rate_limits.json").write_text('{"rate_limits":{"five_hour":{"used_percentage":2}}}')
    monkeypatch.setattr(agent_module, "run_command", lambda *a, **k: "")
    reports = []
    monkeypatch.setattr(agent, "report", lambda *a: reports.append(a))
    with pytest.raises(agent_module.OperationError):
        agent.probe_rate_limits()
    assert not reports


def test_command_errors_do_not_echo_output_or_credentials():
    with pytest.raises(agent_module.OperationError) as exc:
        agent_module.run_command(["/bin/sh", "-c", "echo synthetic-secret; exit 3"])
    assert "synthetic-secret" not in str(exc.value)
    assert "3" in str(exc.value)


def test_commands_do_not_inherit_agent_identity(monkeypatch):
    import sys

    monkeypatch.setenv("COREMAN_AGENT_TOKEN", "synthetic-agent-secret")
    monkeypatch.setenv("COREMAN_API_URL", "https://coreman.invalid")
    monkeypatch.setenv("COREMAN_RELAY_ID", "synthetic-relay")
    result = agent_module.run_command(
        [
            sys.executable,
            "-c",
            "import os,json; print(json.dumps({'agent': "
            "[k for k in os.environ if k.startswith('COREMAN_')], "
            "'path': bool(os.environ.get('PATH'))}))",
        ]
    )
    assert json.loads(result) == {"agent": [], "path": True}


def test_memory_v2_conflict_preflight_delete_and_mtime(agent):
    import hashlib
    import os
    import time

    bot = agent.root / "bot"
    bot.mkdir()
    now = time.time()
    row = {
        "working_dir": str(bot),
        "file_name": "note.md",
        "content": "first",
        "content_hash": hashlib.sha256(b"first").hexdigest(),
        "file_mtime": now - 10,
        "deleted": False,
    }
    assert agent.dispatch({"type": "ping"})["memory_protocol"] == 2
    agent.deploy_memory([row], 2)
    path = agent.memory_dir(str(bot)) / "note.md"
    assert abs(path.stat().st_mtime - row["file_mtime"]) < 0.001
    path.write_text("newer local")
    os.utime(path, (now, now))
    with pytest.raises(agent_module.OperationError, match="不能覆盖"):
        agent.deploy_memory([row], 2)
    assert path.read_text() == "newer local"
    row.update(
        content="", content_hash=hashlib.sha256(b"").hexdigest(), deleted=True, file_mtime=now + 1
    )
    agent.deploy_memory([row], 2)
    assert not path.exists()
    assert agent.deploy_memory([row], 2) == 1


def test_memory_snapshot_does_not_report_or_hold_lock_during_report(agent, monkeypatch):
    bot = agent.root / "bot"
    bot.mkdir()
    agent.deploy_memory([{"working_dir": str(bot), "file_name": "note.md", "content": "note"}])
    seen = []

    def report(endpoint, body):
        assert agent.lock.acquire(blocking=False), "report must release agent operation lock"
        agent.lock.release()
        seen.append(endpoint)
        return {"success": True}

    monkeypatch.setattr(agent, "report", report)
    snapshot = agent.dispatch({"type": "read-memory", "working_dir": str(bot)})
    assert snapshot["memories"][0]["content"] == "note" and seen == []
    assert agent.dispatch({"type": "collect-memory", "working_dir": str(bot)})["count"] == 1
    assert seen == ["memories/collect"]


def test_skill_install_selects_only_requested_catalog_entry(agent, monkeypatch):
    bot = agent.root / "bot"
    bot.mkdir()
    calls = []
    monkeypatch.setattr(
        agent_module, "run_command", lambda command, *args, **kwargs: calls.append(command)
    )
    agent.install_skill(
        {
            "project_dir": str(bot),
            "git_url": "https://github.com/example/tools.git",
            "skill_name": "query",
        }
    )
    assert calls[0][-3:] == ["--skill", "query", "-y"]
    with pytest.raises(agent_module.OperationError):
        agent.install_skill(
            {
                "project_dir": str(bot),
                "git_url": "https://github.com/example/tools.git",
                "skill_name": "--all",
            }
        )


def test_automatic_memory_report_reads_only_assigned_workspaces(agent, monkeypatch):
    mine = agent.root / "mine"
    unrelated = agent.root / "unrelated"
    mine.mkdir()
    unrelated.mkdir()
    for folder in (mine, unrelated):
        agent.deploy_memory(
            [{"working_dir": str(folder), "file_name": "note.md", "content": folder.name}]
        )
    monkeypatch.setattr(agent, "assigned_workspaces", lambda: [str(mine)])
    sent = []
    monkeypatch.setattr(agent, "report", lambda endpoint, body: sent.extend(body["memories"]))
    assert agent.dispatch({"type": "collect-memory"})["count"] == 1
    assert [row["content"] for row in sent] == ["mine"]


def test_skill_token_only_reaches_git_clone_not_installer(agent, monkeypatch):
    bot = agent.root / "token-bot"
    bot.mkdir()
    calls = []
    monkeypatch.setattr(
        agent_module, "run_command", lambda command, *a, **kwargs: calls.append((command, kwargs))
    )
    agent.install_skill(
        {
            "project_dir": str(bot),
            "git_url": "https://github.com/example/tools.git",
            "skill_name": "query",
            "git_access_token": "test-token",
        }
    )
    clone, install = calls
    assert clone[0][:2] == ["git", "clone"]
    assert "test-token" not in str(clone[0])
    assert clone[1]["env_override"]["GIT_CONFIG_COUNT"] == "7"
    assert "env_override" not in install[1]
    assert "test-token" not in str(install)
    assert install[0][4].startswith("/")
    assert install[0][-3:] == ["--skill", "query", "-y"]

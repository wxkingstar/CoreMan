"""管理命令：运行时节点、安装链接、席位、AI 实例与任务总线运行状态。"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from coreman.api.routers import relay_agent, runtime_seats
from coreman.core.db.models import (
    AuditLog,
    OutboxItem,
    ProcessInstance,
    RuntimeInstallLink,
    RuntimeNode,
    Task,
    Team,
)
from tests.api.test_admin_cli import add_user, cli  # noqa: F401
from tests.api.test_runtime import _seed
from tests.integration.worker_helpers import seed_bot

SEAT = "0123456789ab"


async def online_node(db_session, *, seats=True):  # type: ignore[no-untyped-def]
    _, relay, _ = await seed_bot(db_session)
    node = await db_session.get(RuntimeNode, relay.runtime_node_id)
    node.heartbeat_at = datetime.now(UTC)
    node.capabilities = {
        "claude": {
            "installed": True,
            "login": "ready",
            "seat_switch_v1": True,
            "seats": [{"id": "default", "label": "", "account": "a@example.com", "login": "ready"}]
            if seats
            else [],
        },
        "codex": {"installed": False},
    }
    await db_session.commit()
    return node, relay


async def test_node_list_show_set_and_delete(cli, db_session):  # noqa: F811
    node, _ = await online_node(db_session)
    spare = RuntimeNode(
        id=uuid.uuid4(),
        name="spare",
        token_hash="spare-hash",
        workspace_root="/home/ai",
        hostname="spare.example.test",
        username="ai",
        platform="linux",
        architecture="amd64",
        environment="host",
        version="test",
    )
    db_session.add_all([spare, Team(slug="sales", name_zh="销售部")])
    await db_session.commit()
    await add_user(db_session, "alice")
    await add_user(db_session, "ops", "platform_admin")

    code, out, err = await cli("runtimes", "list", "--as", "alice")
    assert code == 0, err
    assert "node-1" in out and "spare" in out and "claude:" in out
    code, out, err = await cli("runtimes", "show", "node-1", "--as", "alice")
    assert code == 0, err
    assert "claude 实例" in out and "a@example.com" in out

    # 普通成员不能改别人的节点
    code, _, err = await cli("runtimes", "set", "node-1", "--draining", "on", "--as", "alice")
    assert code == 1
    code, out, err = await cli(
        "runtimes",
        "set",
        str(node.id)[:8],
        "--name",
        "main",
        "--draining",
        "on",
        "--team",
        "sales",
        "--as",
        "ops",
    )
    assert code == 0, err
    assert "name" in out and "draining" in out and "team_id" in out
    await db_session.refresh(node)
    assert node.name == "main" and node.draining is True and node.team_id is not None
    code, _, err = await cli("runtimes", "set", "main", "--team", "", "--as", "ops")
    assert code == 0, err
    await db_session.refresh(node)
    assert node.team_id is None

    code, _, err = await cli("runtimes", "delete", "spare", "--as", "ops")
    assert code == 1 and "--yes" in err
    code, _, err = await cli("runtimes", "delete", "main", "--yes", "--as", "ops")
    assert code == 1 and "仍有 AI 员工使用" in err
    code, out, err = await cli("runtimes", "delete", "spare", "--yes", "--as", "ops")
    assert code == 0, err
    assert await db_session.get(RuntimeNode, spare.id, populate_existing=True) is None
    actions = set(await db_session.scalars(select(AuditLog.action)))
    assert {"runtime.update", "runtime.delete"} <= actions


async def test_install_links(cli, db_session, tmp_path):  # noqa: F811
    db_session.add(Team(slug="sales", name_zh="销售部"))
    await db_session.commit()
    await add_user(db_session, "alice")
    code, _, err = await cli("runtimes", "link-create", "--team", "sales", "--as", "alice")
    assert code == 1 and "管理员" in err
    code, out, err = await cli(
        "runtimes",
        "link-create",
        "--name",
        "laptop",
        "--visibility",
        "private",
        "--git-host",
        "git.example.com",
        "--max-concurrent",
        "4",
        "--as",
        "alice",
    )
    assert code == 0, err
    assert "curl -fsSL" in out and "/install.sh" in out
    link = await db_session.scalar(select(RuntimeInstallLink))
    assert link.visibility == "private" and link.options["git_hosts"] == ["git.example.com"]
    assert link.options["max_concurrent"] == 4

    code, out, err = await cli("runtimes", "links", "--as", "alice")
    assert code == 0, err
    assert "laptop" in out and "ready" in out
    code, out, err = await cli("runtimes", "link-revoke", str(link.id)[:6], "--as", "alice")
    assert code == 0, err
    await db_session.refresh(link)
    assert link.revoked_at is not None


async def test_seats_login_switch_and_remove(cli, db_session, monkeypatch):  # noqa: F811
    await online_node(db_session)
    await add_user(db_session, "alice")
    await add_user(db_session, "ops", "platform_admin")
    calls = []
    state = {"active": "default", "extra": False}

    def seat_list():  # type: ignore[no-untyped-def]
        rows = [{"id": "default", "label": "", "account": "a@example.com", "login": "ready"}]
        if state["extra"]:
            rows.append({"id": SEAT, "label": "bob", "account": "b@example.com", "login": "ready"})
        return [{**r, "active": r["id"] == state["active"]} for r in rows]

    async def agent(relay, cipher, operation, payload=None):  # type: ignore[no-untyped-def]
        calls.append((operation, payload))
        if operation == "seat-create":
            state["extra"] = True
            return {"success": True, "seat_id": SEAT, "seats": seat_list()}
        if operation == "seat-login-start":
            return {
                "session_id": "login-1",
                "authorization_url": "https://login.example.com/authorize",
                "user_code": "",
                "needs_code": True,
                "expires_in": 900,
            }
        if operation == "seat-activate":
            state["active"] = payload["seat_id"]
        if operation == "seat-remove":
            state["extra"] = False
        return {"success": True, "seats": seat_list()}

    monkeypatch.setattr(runtime_seats, "call_agent", agent)

    code, _, err = await cli(
        "runtimes", "seat-add", "node-1", "claude", "--label", "bob", "--as", "alice"
    )
    assert code == 1 and not calls
    code, out, err = await cli(
        "runtimes", "seat-add", "node-1", "claude", "--label", "bob", "--as", "ops"
    )
    assert code == 0, err
    assert SEAT in out and "b@example.com" in out
    code, out, err = await cli("runtimes", "seats", "node-1", "--as", "ops")
    assert code == 0, err
    assert "bob" in out

    code, out, err = await cli("runtimes", "seat-login", "node-1", "claude", "bob", "--as", "ops")
    assert code == 0, err
    assert "https://login.example.com/authorize" in out and "login-1" in out
    assert "seat-login-complete" in out
    code, out, err = await cli(
        "runtimes",
        "seat-login-complete",
        "node-1",
        "claude",
        "bob",
        "--session",
        "login-1",
        "--code",
        "abc#s",
        "--as",
        "ops",
    )
    assert code == 0, err
    assert calls[-1] == (
        "seat-login-complete",
        {"seat_id": SEAT, "session_id": "login-1", "code": "abc#s"},
    )

    code, _, err = await cli("runtimes", "seat-activate", "node-1", "claude", "bob", "--as", "ops")
    assert code == 1 and "--yes" in err
    code, out, err = await cli(
        "runtimes", "seat-activate", "node-1", "claude", "bob", "--yes", "--as", "ops"
    )
    assert code == 0, err
    assert calls[-1] == ("seat-activate", {"seat_id": SEAT})
    code, _, err = await cli(
        "runtimes", "seat-remove", "node-1", "claude", SEAT, "--yes", "--as", "ops"
    )
    assert code == 0, err
    assert calls[-1] == ("seat-remove", {"seat_id": SEAT})
    code, _, err = await cli(
        "runtimes", "seat-add", "node-1", "codex", "--label", "x", "--as", "ops"
    )
    assert code == 1 and "不存在" in err


async def test_backend_models_usage_status_and_probe(cli, db_session, monkeypatch):  # noqa: F811
    await online_node(db_session)
    await add_user(db_session, "alice")
    await add_user(db_session, "ops", "platform_admin")
    calls = []

    async def agent(relay, cipher, operation, payload=None):  # type: ignore[no-untyped-def]
        calls.append(operation)
        if operation == "status":
            return {
                "success": True,
                "hostname": "relay.test",
                "user": "coreman",
                "active_tasks": [{"bot_key": "sales_bot", "user": "alice", "chat_id": "c1"}],
            }
        return {"success": True, "started": True}

    monkeypatch.setattr(relay_agent, "call_agent", agent)

    code, out, err = await cli("runtimes", "backends", "--as", "alice")
    assert code == 0, err
    assert "node-1" in out and "claude" in out
    code, out, err = await cli("runtimes", "models", "node-1", "claude", "--json", "--as", "alice")
    assert code == 0, err
    assert json.loads(out)["provider"] == "claude"
    code, _, err = await cli("runtimes", "usage", "node-1", "claude", "--as", "alice")
    assert code == 1
    code, out, err = await cli("runtimes", "usage", "node-1", "claude", "--as", "ops")
    assert code == 0, err
    assert "对话数" in out
    code, out, err = await cli("runtimes", "status", "node-1", "claude", "--as", "ops")
    assert code == 0, err
    assert "销售" in out and "c1" in out
    code, out, err = await cli(
        "runtimes", "agent-probe", "node-1", "claude", "rate-limits", "--as", "ops"
    )
    assert code == 0, err
    assert calls == ["status", "probe-rate-limits"]


async def test_session_events_check_access(cli, db_session):  # noqa: F811
    await online_node(db_session)
    await add_user(db_session, "alice")
    code, _, err = await cli(
        "runtimes", "session", "node-1", "claude", str(uuid.uuid4()), "--as", "alice"
    )
    assert code == 1 and err.strip()


async def test_queue_commands(cli, db_session):  # noqa: F811
    bot = await _seed(db_session)
    await add_user(db_session, "lead", "team_lead")
    await add_user(db_session, "ops", "platform_admin")

    code, out, err = await cli("queue", "overview", "--as", "lead")
    assert code == 0, err
    assert "0 / 1" in out and "出站失败" in out
    code, out, err = await cli("queue", "tasks", "--as", "lead")
    assert code == 0, err
    assert "bb（b）" in out and "running" in out
    code, out, err = await cli("queue", "tasks", "--queued", "--as", "lead")
    assert code == 0, err
    assert "fast" in out
    code, out, err = await cli("queue", "instances", "--as", "lead")
    assert code == 0, err
    assert "gateway-wecom" in out and "worker" in out
    code, out, err = await cli("queue", "leases", "--as", "lead")
    assert code == 0, err
    assert "gateway-wecom-a" in out
    code, out, err = await cli("queue", "outbox", "--as", "lead")
    assert code == 0, err
    assert "boom" in out

    item = await db_session.scalar(select(OutboxItem))
    task = await db_session.scalar(select(Task).where(Task.status == "running"))
    code, _, err = await cli("queue", "outbox-retry", str(item.id), "--as", "lead")
    assert code == 1
    code, out, err = await cli("queue", "outbox-retry", str(item.id), "--as", "ops")
    assert code == 0, err
    assert "pending" in out
    code, _, err = await cli("queue", "cancel-task", str(task.id), "--as", "ops")
    assert code == 1 and "--yes" in err
    code, out, err = await cli(
        "queue", "cancel-task", str(task.id), "--reason", "stuck", "--yes", "--as", "ops"
    )
    assert code == 0, err
    await db_session.refresh(task)
    assert task.cancel_reason == "stuck"

    code, _, err = await cli("queue", "drain", "--bot", "bb", "--as", "ops")
    assert code == 1 and "--yes" in err
    code, out, err = await cli("queue", "drain", "--bot", bot.bot_key, "--yes", "--as", "ops")
    assert code == 0, err
    code, out, err = await cli("queue", "drain", "--instance", "worker-a", "--yes", "--as", "ops")
    assert code == 0, err
    worker = await db_session.get(ProcessInstance, "worker-a:h:2:1", populate_existing=True)
    assert worker.drain_requested_at is not None

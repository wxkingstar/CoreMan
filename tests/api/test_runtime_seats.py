"""运行时席位：平台管理员在管理台给节点添加账号、远程登录、切换当前席位、移除。"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from coreman.api.routers import runtime_seats
from coreman.core.db.models import AuditLog, RuntimeNode
from coreman.core.relay.agent_client import AgentError
from tests.api.conftest import login_as
from tests.integration.worker_helpers import seed_bot

SEAT = "0123456789ab"


def seats(active="default", *, extra=True):
    rows = [{"id": "default", "label": "", "account": "a@example.com", "login": "ready"}]
    if extra:
        rows.append({"id": SEAT, "label": "张三", "account": "b@example.com", "login": "ready"})
    return [{**row, "active": row["id"] == active} for row in rows]


async def prepare(client, session, *, supported=True, role="platform_admin"):
    _, relay, _ = await seed_bot(session)
    node = await session.get(RuntimeNode, relay.runtime_node_id)
    node.heartbeat_at = datetime.now(UTC)
    node.capabilities = {
        "claude": {
            "installed": True,
            "login": "ready",
            **({"seat_switch_v1": True, "seats": seats(extra=False)} if supported else {}),
        },
        "codex": {"installed": False},
    }
    await session.commit()
    await login_as(client, session, role=role)
    return node, relay


def fake_node(monkeypatch, *, fail=None):
    calls = []
    state = {"active": "default"}

    async def agent(relay, cipher, operation, payload=None):
        calls.append((operation, payload))
        if fail is not None:
            raise fail
        if operation == "seat-create":
            return {"success": True, "seat_id": SEAT, "seats": seats()}
        if operation == "seat-login-start":
            return {
                "success": True,
                "session_id": "login-1",
                "authorization_url": "https://claude.com/cai/oauth/authorize?code=true&state=s",
                "user_code": "",
                "needs_code": True,
                "expires_in": 900,
            }
        if operation == "seat-activate":
            state["active"] = payload["seat_id"]
        if operation == "seat-remove":
            return {"success": True, "seats": seats(state["active"], extra=False)}
        return {"success": True, "seats": seats(state["active"])}

    monkeypatch.setattr(runtime_seats, "call_agent", agent)
    return calls


def base(node, provider="claude"):
    return f"/api/admin/runtime-nodes/{node.id}/{provider}/seats"


async def test_admin_adds_signs_in_and_switches_a_seat(client, db_session, monkeypatch):
    node, relay = await prepare(client, db_session)
    calls = fake_node(monkeypatch)
    relay.rate_limit_5h_used_pct, relay.rate_limit_7d_used_pct = Decimal("100"), Decimal("80")
    relay.rate_limit_5h_resets_at = datetime(2026, 11, 1, 12, tzinfo=UTC)
    await db_session.commit()

    created = await client.post(base(node), json={"label": "张三"})
    assert created.status_code == 201, created.text
    assert created.json()["data"]["seat_id"] == SEAT
    assert calls[-1] == ("seat-create", {"label": "张三"})
    await db_session.refresh(node)
    assert [s["id"] for s in node.capabilities["claude"]["seats"]] == ["default", SEAT]

    started = await client.post(f"{base(node)}/{SEAT}/login")
    assert started.status_code == 200, started.text
    assert started.json()["data"]["needs_code"] is True
    assert started.json()["data"]["authorization_url"].startswith("https://claude.com/")
    done = await client.post(
        f"{base(node)}/{SEAT}/login/complete", json={"session_id": "login-1", "code": "abc#s"}
    )
    assert done.status_code == 200, done.text
    assert calls[-1] == (
        "seat-login-complete",
        {"seat_id": SEAT, "session_id": "login-1", "code": "abc#s"},
    )

    switched = await client.post(f"{base(node)}/{SEAT}/activate")
    assert switched.status_code == 200, switched.text
    assert [s["active"] for s in switched.json()["data"]["seats"]] == [False, True]
    await db_session.refresh(relay)
    # 新席位没有采集过额度：清空；旧席位的额度存起来，切回时还原。
    assert relay.rate_limit_5h_used_pct is None and relay.rate_limit_7d_used_pct is None
    assert relay.seat_quotas["default"]["rate_limit_5h_used_pct"] == 100.0
    relay.rate_limit_7d_used_pct = Decimal("3")
    await db_session.commit()

    back = await client.post(f"{base(node)}/default/activate")
    assert back.status_code == 200, back.text
    await db_session.refresh(relay)
    assert relay.rate_limit_5h_used_pct == Decimal("100")
    assert relay.rate_limit_5h_resets_at == datetime(2026, 11, 1, 12, tzinfo=UTC)
    assert relay.seat_quotas[SEAT]["rate_limit_7d_used_pct"] == 3.0
    assert "default" not in relay.seat_quotas

    removed = await client.delete(f"{base(node)}/{SEAT}")
    assert removed.status_code == 200, removed.text
    await db_session.refresh(relay)
    assert relay.seat_quotas == {}
    actions = (
        await db_session.scalars(
            select(AuditLog.action).where(AuditLog.target_id == str(node.id)).order_by(AuditLog.id)
        )
    ).all()
    assert actions == [
        "runtime.seat_create",
        "runtime.seat_login",
        "runtime.seat_activate",
        "runtime.seat_activate",
        "runtime.seat_remove",
    ]


async def test_only_platform_admins_manage_seats(client, db_session, monkeypatch):
    node, _ = await prepare(client, db_session, role="ai_committee")
    calls = fake_node(monkeypatch)
    response = await client.post(base(node), json={"label": "张三"})
    assert response.status_code == 403
    assert calls == []


async def test_old_or_offline_runtime_is_refused(client, db_session, monkeypatch):
    node, _ = await prepare(client, db_session, supported=False)
    calls = fake_node(monkeypatch)
    response = await client.post(base(node), json={"label": "张三"})
    assert response.status_code == 409 and "升级" in response.json()["message"]
    node.capabilities = {"claude": {"installed": True, "seat_switch_v1": True}}
    node.heartbeat_at = datetime.now(UTC) - timedelta(hours=1)
    await db_session.commit()
    response = await client.post(base(node), json={"label": "张三"})
    assert response.status_code == 409 and "在线" in response.json()["message"]
    # 这台节点上没有 Codex 实例。
    response = await client.post(base(node, "codex"), json={"label": "张三"})
    assert response.status_code == 404
    assert calls == []


async def test_node_errors_and_bad_ids_are_reported(client, db_session, monkeypatch):
    node, _ = await prepare(client, db_session)
    fake_node(
        monkeypatch,
        fail=AgentError(
            "x", code="operation_failed", detail="还没有在浏览器里完成授权，完成后再确认"
        ),
    )
    response = await client.post(
        f"{base(node)}/{SEAT}/login/complete", json={"session_id": "login-1"}
    )
    assert response.status_code == 422 and "浏览器" in response.json()["message"]
    response = await client.post(f"{base(node)}/ABC/activate")
    assert response.status_code == 422


def test_heartbeat_keeps_reported_seats():
    from coreman.api.routers.runtime_protocol import Capability

    cap = Capability.model_validate(
        {"installed": True, "seat_switch_v1": True, "seats": seats(SEAT)}
    ).model_dump()
    assert cap["seats"][1] == {
        "id": SEAT,
        "label": "张三",
        "account": "b@example.com",
        "login": "ready",
        "active": True,
    }

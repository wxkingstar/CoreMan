from datetime import UTC, datetime

from coreman.core.db.models import ChatLog, RelayServer
from tests.api.conftest import login_as


async def test_live_status_requires_manager_and_usage_distinguishes_missing_values(
    client, db_session, monkeypatch
):
    from coreman.api.routers import relay_agent

    user = await login_as(client, db_session, role="member")
    relay = RelayServer(
        name="agent", host="10.0.0.9", clawrelay_port=50009, model_provider="claude"
    )
    db_session.add(relay)
    await db_session.commit()
    path = f"/api/admin/relay-servers/{relay.id}"
    calls = []

    async def fake_call(relay, cipher, operation):
        calls.append(operation)
        return {"success": True, "active_tasks": []}

    monkeypatch.setattr(relay_agent, "call_agent", fake_call)
    assert (await client.get(path + "/live-status")).status_code == 403
    assert (await client.get(path + "/today-usage")).status_code == 403
    assert not calls
    user.role = "platform_admin"
    await db_session.commit()
    assert (await client.get(path + "/live-status")).status_code == 200
    assert (await client.post(path + "/agent-probe", json={"operation": "pull"})).status_code == 422
    assert (
        await client.post(path + "/agent-probe", json={"operation": "health-check"})
    ).status_code == 200
    assert calls == ["status", "health-check"]
    usage = (await client.get(path + "/today-usage")).json()["data"]
    assert usage["total"] == 0 and usage["input_tokens"] is None and usage["cost_usd"] is None
    db_session.add(
        ChatLog(
            bot_id=relay.id,
            bot_key="test",
            platform="wecom",
            relay_server_id=relay.id,
            chat_type="single",
            message_type="text",
            status="success",
            request_at=datetime.now(UTC),
            input_tokens=10,
            output_tokens=3,
        )
    )
    await db_session.commit()
    usage = (await client.get(path + "/today-usage")).json()["data"]
    assert usage["total"] == 1 and usage["input_tokens"] == 10 and usage["output_tokens"] == 3
    assert usage["cost_usd"] is None and usage["cost_reported"] == 0 and usage["timezone"] == "UTC"

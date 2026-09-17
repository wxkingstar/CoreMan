import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import RelayServer
from coreman.core.relay import probe
from coreman.core.relay.client import RelayClient, RelayHealth
from tests.api.conftest import login_as
from tests.fakes.runtime_node import add_node_relay

# 独立中继实例的旧字段：输出契约里不再出现。
LEGACY_FIELDS = {
    "host",
    "clawrelay_port",
    "agent_port",
    "ssh_user",
    "runtime_env",
    "chroot_path",
    "runtime_user",
    "has_agent_token",
}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    class Fake:
        async def health(self) -> RelayHealth:
            return RelayHealth("healthy", None, 5, backend="claude", version="2.2.0", mode="v1")

        async def models(self) -> list[str]:
            return ["claude-sonnet-5", "claude-extra"]

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(probe, "make_client", lambda relay: Fake())


async def test_list_probe_and_models_without_legacy_fields(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    relay = await add_node_relay(db_session, name="claude01")
    await db_session.commit()
    await login_as(client, db_session, role="ai_committee")
    items = (await client.get("/api/admin/relay-servers")).json()["data"]["items"]
    assert len(items) == 1
    out = items[0]
    assert not LEGACY_FIELDS & set(out)
    assert out["name"] == "claude01 / claude"
    assert out["runtime_node_id"] == str(relay.runtime_node_id)
    assert out["workspace_root"] == "/data/skills"
    assert out["relay_url"] == f"runtime://{relay.runtime_node_id}/claude"
    probe_r = await client.post(f"/api/admin/relay-servers/{relay.id}/probe")
    assert probe_r.status_code == 200, probe_r.text
    data = probe_r.json()["data"]
    assert data["health"]["status"] == "healthy" and data["added_models"] == ["claude-extra"]
    assert not LEGACY_FIELDS & set(data["relay"])
    models = (await client.get(f"/api/admin/relay-servers/{relay.id}/models")).json()["data"]
    assert models["provider"] == "claude" and "claude-extra" in models["models"]
    assert models["default"] == "claude-sonnet-5"


async def test_manual_relay_management_routes_are_gone(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    relay = await add_node_relay(db_session)
    await db_session.commit()
    await login_as(client, db_session, role="platform_admin")
    base = "/api/admin/relay-servers"
    assert (await client.post(base, json={"name": "manual"})).status_code == 405
    for method, path in [
        ("GET", f"{base}/{relay.id}"),
        ("PUT", f"{base}/{relay.id}"),
        ("DELETE", f"{base}/{relay.id}"),
        ("POST", f"{base}/{relay.id}/agent-token"),
        ("GET", f"{base}/team-load"),
    ]:
        response = await client.request(method, path)
        assert response.status_code in {404, 405}, (method, path, response.status_code)


async def test_visibility_permissions_and_standalone_rows_hidden(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await add_node_relay(db_session, name="pub")
    qa = await add_node_relay(db_session, name="qa", visibility="admins")
    db_session.add(RelayServer(name="standalone", model_provider="claude"))
    await db_session.commit()
    await login_as(client, db_session, role="member")
    items = (await client.get("/api/admin/relay-servers")).json()["data"]["items"]
    assert [x["name"] for x in items] == ["pub / claude"]
    assert (await client.get(f"/api/admin/relay-servers/{qa.id}/models")).status_code == 404
    assert (await client.post(f"/api/admin/relay-servers/{qa.id}/probe")).status_code == 403
    await login_as(client, db_session, role="platform_admin")
    items = (await client.get("/api/admin/relay-servers")).json()["data"]["items"]
    assert sorted(x["name"] for x in items) == ["pub / claude", "qa / claude"]


async def test_probe_standalone_relay_is_422(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未绑定节点的实例没有任何可达的传输方式：显式探测按 422 回，不是 500。"""
    relay = RelayServer(name="standalone", model_provider="claude")
    db_session.add(relay)
    await db_session.commit()
    await login_as(client, db_session, role="platform_admin")
    monkeypatch.setattr(probe, "make_client", RelayClient.for_relay)
    r = await client.post(f"/api/admin/relay-servers/{relay.id}/probe")
    assert r.status_code == 422, r.text
    assert r.json()["message"] == "运行时未绑定节点，无法探测"

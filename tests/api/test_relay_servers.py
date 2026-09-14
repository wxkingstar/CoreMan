import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import AuditLog, Bot, RelayServer, Team
from coreman.core.relay import probe
from coreman.core.relay.client import RelayHealth
from tests.api.conftest import login_as

RELAY = {
    "name": "claude01",
    "host": "10.0.0.1",
    "clawrelay_port": 50009,
    "agent_port": 52123,
    "ssh_user": "claude01",
    "runtime_env": "chroot",
    "chroot_path": "/srv/claude01",
    "runtime_user": "claude01",
    "model_provider": "claude",
    "supported_models_mode": "inherit",
    "supported_models": None,
    "team_id": None,
    "visibility": "all",
    "description": "测试",
    "is_active": True,
}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    class Fake:
        async def health(self) -> RelayHealth:
            return RelayHealth("healthy", None, 5, backend="claude", version="2.2.0", mode="v1")

        async def models(self) -> list[str]:
            return ["vllm/claude-sonnet-4-6", "vllm/claude-extra"]

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(probe, "make_client", lambda relay: Fake())


async def test_crud_probe_models_token(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    await login_as(client, db_session, role="ai_committee")
    r = await client.post("/api/admin/relay-servers", json=RELAY)
    assert r.status_code == 201, r.text
    out = r.json()["data"]
    assert (
        out["relay_url"] == "http://10.0.0.1:50009"
        and out["health_status"] == "healthy"
        and out["has_agent_token"] is False
    )
    assert (
        "vllm/claude-extra" in out["effective_models"]
        and out["default_model"] == "vllm/claude-sonnet-4-6"
    )
    assert (await client.post("/api/admin/relay-servers", json=RELAY)).status_code == 409
    rid = out["id"]
    upd = await client.put(
        f"/api/admin/relay-servers/{rid}",
        json={
            **RELAY,
            "description": "改",
            "supported_models_mode": "restricted",
            "supported_models": ["vllm/claude-sonnet-4-6"],
        },
        headers={"If-Match": f'"{out["version"]}"'},
    )
    assert upd.status_code == 200 and upd.json()["data"]["effective_models"] == [
        "vllm/claude-sonnet-4-6"
    ]
    assert (
        await client.put(f"/api/admin/relay-servers/{rid}", json=RELAY, headers={"If-Match": '"1"'})
    ).status_code == 409
    models = (await client.get(f"/api/admin/relay-servers/{rid}/models")).json()["data"]
    assert models == {
        "provider": "claude",
        "mode": "restricted",
        "models": ["vllm/claude-sonnet-4-6"],
        "default": "vllm/claude-sonnet-4-6",
    }
    probe_r = await client.post(f"/api/admin/relay-servers/{rid}/probe")
    assert probe_r.status_code == 200 and probe_r.json()["data"]["health"]["status"] == "healthy"
    tok = await client.post(f"/api/admin/relay-servers/{rid}/agent-token")
    assert tok.status_code == 200 and len(tok.json()["data"]["token"]) >= 32
    row = (await db_session.execute(select(RelayServer))).scalar_one()
    assert (
        row.agent_token_enc
        and row.agent_token_enc.startswith("enc:v1:")
        and tok.json()["data"]["token"] not in row.agent_token_enc
    )
    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "relay.agent_token"))
    ).scalar_one()
    assert audit.diff == {"has_agent_token": [False, True]}
    assert (await client.get(f"/api/admin/relay-servers/{rid}")).json()["data"][
        "has_agent_token"
    ] is True
    assert (await client.delete(f"/api/admin/relay-servers/{rid}")).status_code == 200


async def test_visibility_and_permissions(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    db_session.add_all(
        [
            RelayServer(name="pub", host="h1", clawrelay_port=1, model_provider="claude"),
            RelayServer(
                name="qa", host="h2", clawrelay_port=1, model_provider="claude", visibility="admins"
            ),
        ]
    )
    await db_session.commit()
    await login_as(client, db_session, role="member")
    names = [
        x["name"] for x in (await client.get("/api/admin/relay-servers")).json()["data"]["items"]
    ]
    assert names == ["pub"]
    qa_id = str(
        (
            await db_session.execute(select(RelayServer.id).where(RelayServer.name == "qa"))
        ).scalar_one()
    )
    assert (await client.get(f"/api/admin/relay-servers/{qa_id}")).status_code == 404
    assert (await client.post("/api/admin/relay-servers", json=RELAY)).status_code == 403
    await login_as(client, db_session, role="platform_admin")
    assert len((await client.get("/api/admin/relay-servers")).json()["data"]["items"]) == 2


async def test_delete_blocked_by_bot_and_team_load(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await login_as(client, db_session, role="platform_admin")
    team = Team(slug="t1", name_zh="团队一")
    db_session.add(team)
    await db_session.flush()
    relay = RelayServer(
        name="r1", host="h", clawrelay_port=1, model_provider="claude", team_id=team.id
    )
    db_session.add(relay)
    await db_session.flush()
    db_session.add(
        Bot(
            bot_key="b1",
            platform="wecom",
            name="b",
            created_by=admin.id,
            relay_server_id=relay.id,
            model="m",
            working_dir="/d",
            credentials_enc="enc",
        )
    )
    await db_session.commit()
    assert (await client.delete(f"/api/admin/relay-servers/{relay.id}")).status_code == 409
    load = (await client.get("/api/admin/relay-servers/team-load")).json()["data"]
    assert load["teams"][0] == {
        "team_id": str(team.id),
        "team_name": "团队一",
        "relay_count": 1,
        "relay_names": ["r1"],
        "bot_count": 0,
    }
    assert load["unassigned_bot_count"] == 1 and load["unassigned_user_count"] >= 1


async def test_update_relay_rejects_orphaning_bound_bots(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """改基座/白名单会让有效模型集变化：绑着的机器人如果掉到集合外，必须 409 点名拦住。"""
    admin = await login_as(client, db_session, role="platform_admin")
    created = (await client.post("/api/admin/relay-servers", json=RELAY)).json()["data"]
    rid, ver = created["id"], created["version"]
    bot = Bot(
        bot_key="sales_bot",
        platform="wecom",
        name="b",
        created_by=admin.id,
        relay_server_id=uuid.UUID(rid),
        model="vllm/claude-sonnet-4-6",
        working_dir="/d",
        credentials_enc="enc",
    )
    db_session.add(bot)
    await db_session.commit()
    codex = {**RELAY, "model_provider": "codex"}
    r = await client.put(
        f"/api/admin/relay-servers/{rid}", json=codex, headers={"If-Match": f'"{ver}"'}
    )
    assert r.status_code == 409 and "sales_bot" in r.json()["message"]
    # 被拦下的请求不能留下任何改动
    relay = (await db_session.execute(select(RelayServer))).scalar_one()
    await db_session.refresh(relay)
    assert relay.model_provider == "claude" and relay.version == ver
    # 机器人迁走之后，同一个 PUT 就该放行
    bot.relay_server_id = None
    await db_session.commit()
    ok = await client.put(
        f"/api/admin/relay-servers/{rid}", json=codex, headers={"If-Match": f'"{ver}"'}
    )
    assert ok.status_code == 200, ok.text
    # 有效集没变（只改描述）时不校验：机器人的模型早就不在集内也照样能改
    bot.relay_server_id = uuid.UUID(rid)
    await db_session.commit()
    only_desc = await client.put(
        f"/api/admin/relay-servers/{rid}",
        json={**codex, "description": "只改描述"},
        headers={"If-Match": f'"{ok.json()["data"]["version"]}"'},
    )
    assert only_desc.status_code == 200, only_desc.text
    assert only_desc.json()["data"]["description"] == "只改描述"


async def test_probe_failure_does_not_fail_create(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """best-effort 探测：health 成功（relay 已改脏）后 models 抛异常，创建仍须 201。"""
    closed: list[bool] = []

    class HalfBroken:
        async def health(self) -> RelayHealth:
            return RelayHealth("healthy", None, 5, backend="claude", version="2.2.0", mode="v1")

        async def models(self) -> list[str]:
            raise RuntimeError("boom")

        async def aclose(self) -> None:
            closed.append(True)

    monkeypatch.setattr(probe, "make_client", lambda relay: HalfBroken())
    await login_as(client, db_session, role="ai_committee")
    r = await client.post("/api/admin/relay-servers", json=RELAY)
    assert r.status_code == 201, r.text
    assert r.json()["data"]["health_status"] == "unknown" and closed == [True]


async def test_team_load_hides_admins_only_relays(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """team-load 与列表口径一致：admins-only 实例的存在与名字不能透给普通成员。"""
    team = Team(slug="t2", name_zh="团队二")
    db_session.add(team)
    await db_session.flush()
    db_session.add(
        RelayServer(
            name="qa-only",
            host="h9",
            clawrelay_port=1,
            model_provider="claude",
            team_id=team.id,
            visibility="admins",
        )
    )
    await db_session.commit()

    await login_as(client, db_session, role="member")
    load = (await client.get("/api/admin/relay-servers/team-load")).json()["data"]
    row = next(t for t in load["teams"] if t["team_id"] == str(team.id))
    assert row["relay_count"] == 0 and row["relay_names"] == []

    await login_as(client, db_session, role="platform_admin")
    load = (await client.get("/api/admin/relay-servers/team-load")).json()["data"]
    row = next(t for t in load["teams"] if t["team_id"] == str(team.id))
    assert row["relay_count"] == 1 and row["relay_names"] == ["qa-only"]


async def test_invalid_host_rejected(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    """httpx 建不出客户端的 host（含控制字符）必须在入参就 422，不能先落库再 500。"""
    await login_as(client, db_session, role="ai_committee")
    r = await client.post("/api/admin/relay-servers", json={**RELAY, "host": "exa\tmple"})
    assert r.status_code == 422, r.text
    assert (await db_session.execute(select(RelayServer))).scalars().all() == []


async def test_make_client_failure_does_not_fail_create(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """make_client 自身抛异常也算探测失败：创建仍 201，健康列保持 unknown。"""

    def boom(relay: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(probe, "make_client", boom)
    await login_as(client, db_session, role="ai_committee")
    r = await client.post("/api/admin/relay-servers", json=RELAY)
    assert r.status_code == 201, r.text
    assert r.json()["data"]["health_status"] == "unknown"


async def test_probe_endpoint_make_client_failure_is_422(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式探测时 make_client 抛异常是「地址无效」，按 422 回，不是 500。"""
    relay = RelayServer(name="bad", host="h8", clawrelay_port=1, model_provider="claude")
    db_session.add(relay)
    await db_session.commit()
    await login_as(client, db_session, role="platform_admin")

    def boom(relay: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(probe, "make_client", boom)
    r = await client.post(f"/api/admin/relay-servers/{relay.id}/probe")
    assert r.status_code == 422, r.text
    assert r.json()["message"] == "运行时地址无效"

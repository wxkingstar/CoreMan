import base64

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.secrets import CREDENTIALS_AAD, ENV_AAD, decrypt_json
from coreman.core.crypto import Cipher
from coreman.core.db.models import AuditLog, Bot, BotMember, RelayServer, Team
from tests.api.conftest import MASTER_KEY, login_as, login_existing
from tests.fakes.runtime_node import attach_node


def _bot_body(**over: object) -> dict[str, object]:
    body: dict[str, object] = {
        "bot_key": "sales_bot",
        "platform": "wecom",
        "name": "销售助手",
        "description": "卖货",
        "relay_server_id": None,
        "model": "claude-sonnet-4-6",
        "working_dir": "/data/skills/sales_bot",
        "system_prompt": "你是销售",
        "verbosity_level": 2,
        "effort_level": "high",
        "sse_timeout_seconds": 3600,
        "agent_timeout_seconds": None,
        "credentials": {"bot_id": "wecom-bot-id-1", "secret": "wecom-secret-value"},
        "env_vars": {"DB_PASSWORD": "pw-123456", "API_BASE": "https://x"},
        "welcome_message": None,
        "notify_webhook_url": None,
        "enabled": True,
    }
    body.update(over)
    return body


async def _team(db_session: AsyncSession, slug: str = "sales") -> Team:
    t = Team(slug=slug, name_zh=slug)
    db_session.add(t)
    await db_session.commit()
    return t


async def test_create_get_masking_and_roles(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    team = await _team(db_session)
    await login_as(client, db_session, role="member", team_id=team.id, login_name="creator")
    r = await client.post("/api/admin/bots", json=_bot_body())
    assert r.status_code == 201, r.text
    out = r.json()["data"]
    assert (
        out["working_dir"] == "/data/skills/sales_bot"
        and out["backend"] == "claude"
        and out["team_id"] == str(team.id)
    )
    assert out["permissions"]["role"] == "creator" and out["credentials"] == {
        "bot_id": "we••••-1",
        "secret": "we••••ue",
    }
    assert (
        out["env_vars"] == {"DB_PASSWORD": "pw••••56", "API_BASE": "ht••••/x"}
        and "env_vars_full" not in out
    )
    row = (await db_session.execute(select(Bot))).scalar_one()
    cipher = Cipher(base64.b64decode(MASTER_KEY))
    assert decrypt_json(cipher, row.credentials_enc, CREDENTIALS_AAD)["secret"] == (
        "wecom-secret-value"
    )
    assert decrypt_json(cipher, row.env_vars_enc, ENV_AAD)["DB_PASSWORD"] == "pw-123456"
    assert row.merged_system_prompt == "你是销售"
    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "bot.create"))
    ).scalar_one()
    assert (
        audit.diff["credentials"] == ["***", "***"]
        and audit.diff["env_vars"] == ["***", "***"]
        and "wecom-secret" not in str(audit.diff)
    )
    # system_prompt 受 can_view_sensitive 管，而审计对 manager 可读：diff 里也只能是 ***
    assert audit.diff["system_prompt"] == ["***", "***"] and "你是销售" not in str(audit.diff)
    # 同团队 team_lead 看敏感字段但不能编辑；外团队 member 看不到；ai_committee 看 env 明文
    await login_as(client, db_session, role="team_lead", team_id=team.id)
    got = (await client.get(f"/api/admin/bots/{out['id']}")).json()["data"]
    assert (
        got["system_prompt"] == "你是销售"
        and got["permissions"]["can_edit"] is False
        and got["permissions"]["can_toggle"] is True
    )
    other = await _team(db_session, "other")
    await login_as(client, db_session, role="member", team_id=other.id)
    got = (await client.get(f"/api/admin/bots/{out['id']}")).json()["data"]
    assert "system_prompt" not in got and "credentials" not in got and got["name"] == "销售助手"
    await login_as(client, db_session, role="ai_committee")
    got = (await client.get(f"/api/admin/bots/{out['id']}")).json()["data"]
    assert (
        got["env_vars_full"] == {"DB_PASSWORD": "pw-123456", "API_BASE": "https://x"}
        and "system_prompt" not in got
    )


async def test_create_validations(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    await login_as(client, db_session, role="member", team_id=None)
    # member 无团队
    assert (await client.post("/api/admin/bots", json=_bot_body())).status_code == 403
    team = await _team(db_session)
    await login_as(client, db_session, role="member", team_id=team.id)
    assert (
        await client.post("/api/admin/bots", json=_bot_body(bot_key="Bad Key"))
    ).status_code == 422
    assert (
        await client.post("/api/admin/bots", json=_bot_body(model="not/in-catalog"))
    ).status_code == 422
    # 目录未标 supports_xhigh
    assert (
        await client.post("/api/admin/bots", json=_bot_body(effort_level="xhigh"))
    ).status_code == 422
    assert (
        await client.post("/api/admin/bots", json=_bot_body(credentials={"bot_id": "x"}))
    ).status_code == 422
    assert (
        await client.post("/api/admin/bots", json=_bot_body(env_vars={"bad-key": "v"}))
    ).status_code == 422
    assert (
        await client.post(
            "/api/admin/bots", json=_bot_body(credentials={"bot_id": "x", "secret": "ab••••yz"})
        )
    ).status_code == 422
    assert (
        await client.post("/api/admin/bots", json=_bot_body(notify_webhook_url="ht••••-1"))
    ).status_code == 422
    # 工作目录会原样下发当运行根，.. 能爬出去
    assert (
        await client.post("/api/admin/bots", json=_bot_body(working_dir="/data/../etc"))
    ).status_code == 422
    assert (await client.post("/api/admin/bots", json=_bot_body())).status_code == 201
    assert (await client.post("/api/admin/bots", json=_bot_body())).status_code == 409


async def test_notify_webhook_is_treated_as_a_credential(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """企微群机器人 webhook 的 key 就写在 URL 里，它本身就是凭证：列表不带、详情只给脱敏值、
    回传脱敏值视为未改、审计只记 ***。"""
    hook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcd-secret-key-1"
    team = await _team(db_session)
    creator = await login_as(
        client, db_session, role="member", team_id=team.id, login_name="creator"
    )
    r = await client.post("/api/admin/bots", json=_bot_body(notify_webhook_url=hook))
    assert r.status_code == 201, r.text
    created = r.json()["data"]
    bid, ver, masked = created["id"], created["version"], created["notify_webhook_url"]
    assert masked == "ht••••-1" and hook not in str(created)
    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "bot.create"))
    ).scalar_one()
    assert audit.diff["notify_webhook_url"] == ["***", "***"]
    assert "abcd-secret-key" not in str(audit.diff)
    # 列表（include_sensitive=False）对谁都不带这个键
    listed = (await client.get("/api/admin/bots", params={"scope": "all"})).json()["data"]["items"]
    assert listed and all("notify_webhook_url" not in b for b in listed)
    # 外团队成员连脱敏值都拿不到
    other = await _team(db_session, "other")
    await login_as(client, db_session, role="member", team_id=other.id)
    got = (await client.get(f"/api/admin/bots/{bid}")).json()["data"]
    assert "notify_webhook_url" not in got
    # 回传脱敏值 = 这一项没改：库里 URL 不变，审计里也不出现这个键
    await login_existing(client, db_session, creator)
    r = await client.patch(
        f"/api/admin/bots/{bid}",
        json={"name": "新名", "notify_webhook_url": masked},
        headers={"If-Match": f'"{ver}"'},
    )
    assert r.status_code == 200, r.text
    ver = r.json()["data"]["version"]
    row = (await db_session.execute(select(Bot))).scalar_one()
    await db_session.refresh(row)
    assert row.notify_webhook_url == hook
    upd = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "bot.update"))
    ).scalar_one()
    assert "notify_webhook_url" not in upd.diff and upd.diff["name"] == ["销售助手", "新名"]
    # 换成新地址：库里更新，审计仍只记 ***
    r = await client.patch(
        f"/api/admin/bots/{bid}",
        json={"notify_webhook_url": hook + "-2"},
        headers={"If-Match": f'"{ver}"'},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["notify_webhook_url"] == "ht••••-2"
    await db_session.refresh(row)
    assert row.notify_webhook_url == hook + "-2"
    diffs = [
        a.diff
        for a in (
            await db_session.execute(
                select(AuditLog).where(AuditLog.action == "bot.update").order_by(AuditLog.id)
            )
        ).scalars()
    ]
    assert diffs[-1]["notify_webhook_url"] == ["***", "***"]
    assert "abcd-secret-key" not in str(diffs)


async def test_validation_error_never_echoes_values(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """env_vars 是整体校验器：一个坏键失败，pydantic 的 `input` 会把整个 dict（含明文密码）
    带进 422 明细。全局 handler 只回 loc/msg/type，值一律不出现。"""
    team = await _team(db_session)
    await login_as(client, db_session, role="member", team_id=team.id)
    r = await client.post(
        "/api/admin/bots", json=_bot_body(env_vars={"DB_PASSWORD": "pw-123456", "bad-key": "v"})
    )
    assert r.status_code == 422
    assert "pw-123456" not in r.text
    first = r.json()["errors"][0]
    assert "loc" in first and "msg" in first
    assert set(first) <= {"loc", "msg", "type"}


async def test_env_vars_reject_runtime_control_variables(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """模型端点、解释器启动项等控制类变量：新建与修改都 422，明细落在 env_vars 字段上且只点名键。"""
    team = await _team(db_session)
    await login_as(client, db_session, role="member", team_id=team.id)
    r = await client.post(
        "/api/admin/bots",
        json=_bot_body(
            env_vars={"ANTHROPIC_BASE_URL": "https://attacker.example", "NODE_OPTIONS": "-r x"}
        ),
    )
    assert r.status_code == 422, r.text
    err = r.json()["errors"][0]
    assert err["loc"] == ["body", "env_vars"]
    assert "ANTHROPIC_BASE_URL, NODE_OPTIONS" in err["msg"]
    assert "attacker.example" not in r.text
    bot = (await client.post("/api/admin/bots", json=_bot_body())).json()["data"]
    r = await client.patch(
        f"/api/admin/bots/{bot['id']}",
        json={"env_vars": {"DB_PASSWORD": "pw••••56", "LD_PRELOAD": "/tmp/x.so"}},
        headers={"If-Match": str(bot["version"])},
    )
    assert r.status_code == 422 and "LD_PRELOAD" in r.json()["errors"][0]["msg"]


async def test_relay_url_follows_relay_visibility(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """绑在 visibility='admins' 的 relay 上的机器人：普通成员只该看到 relay 名字，不该
    拿到 http://host:port（relay_servers 对这类实例连存在性都不透）。"""
    team = await _team(db_session)
    relay = RelayServer(name="hidden", model_provider="claude", visibility="admins")
    db_session.add(relay)
    await attach_node(db_session, relay)
    await db_session.commit()
    relay_id, relay_url = str(relay.id), relay.relay_url
    await login_as(client, db_session, role="ai_committee")
    r = await client.post(
        "/api/admin/bots", json=_bot_body(relay_server_id=relay_id, team_id=str(team.id))
    )
    assert r.status_code == 201, r.text
    bid = r.json()["data"]["id"]
    await login_as(client, db_session, role="member", team_id=team.id)
    got = (await client.get(f"/api/admin/bots/{bid}")).json()["data"]
    assert got["relay_name"] == "hidden / claude" and got["relay_url"] is None
    listed = (await client.get("/api/admin/bots", params={"scope": "team"})).json()["data"]["items"]
    assert [(b["relay_name"], b["relay_url"]) for b in listed] == [("hidden / claude", None)]
    await login_as(client, db_session, role="platform_admin")
    got = (await client.get(f"/api/admin/bots/{bid}")).json()["data"]
    assert got["relay_url"] == relay_url and relay_url.startswith("runtime://")


async def test_relay_policy_on_create(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    team_a, team_b = await _team(db_session, "a"), await _team(db_session, "b")
    relays = [
        RelayServer(name="pub", model_provider="claude"),
        RelayServer(name="teamb", model_provider="claude", team_id=team_b.id),
        RelayServer(name="codex", model_provider="codex"),
        RelayServer(name="off", model_provider="claude", is_active=False),
    ]
    db_session.add_all(relays)
    for relay in relays:
        await attach_node(db_session, relay)
    # 未绑定运行时节点的独立实例不能再承接机器人。
    db_session.add(RelayServer(name="standalone", model_provider="claude"))
    await db_session.commit()
    ids = {r.name: str(r.id) for r in (await db_session.execute(select(RelayServer))).scalars()}
    await login_as(client, db_session, role="member", team_id=team_a.id)
    assert (
        await client.post("/api/admin/bots", json=_bot_body(relay_server_id=ids["pub"]))
    ).status_code == 201
    assert (
        await client.post(
            "/api/admin/bots", json=_bot_body(bot_key="b2", relay_server_id=ids["teamb"])
        )
    ).status_code == 422
    assert (
        await client.post(
            "/api/admin/bots", json=_bot_body(bot_key="b3", relay_server_id=ids["off"])
        )
    ).status_code == 422
    # claude 模型不在 codex relay 有效集
    assert (
        await client.post(
            "/api/admin/bots", json=_bot_body(bot_key="b4", relay_server_id=ids["codex"])
        )
    ).status_code == 422
    r = await client.post(
        "/api/admin/bots",
        json=_bot_body(bot_key="b5", relay_server_id=ids["codex"], model="codex/gpt-5.5"),
    )
    assert (
        r.status_code == 201
        and r.json()["data"]["backend"] == "codex"
        and r.json()["data"]["relay_name"] == "codex / codex"
    )
    assert (
        await client.post(
            "/api/admin/bots", json=_bot_body(bot_key="b7", relay_server_id=ids["standalone"])
        )
    ).status_code == 422
    # 工作目录只按节点项目主目录校验：必须是其下的绝对路径。
    for bad in ("/srv/other/b8", "/data/skills", "data/skills/b8"):
        assert (
            await client.post(
                "/api/admin/bots",
                json=_bot_body(bot_key="b8", relay_server_id=ids["pub"], working_dir=bad),
            )
        ).status_code == 422
    await login_as(client, db_session, role="ai_committee")
    assert (
        await client.post(
            "/api/admin/bots",
            json=_bot_body(bot_key="b6", relay_server_id=ids["teamb"], team_id=str(team_a.id)),
        )
    ).status_code == 201


async def test_list_scopes_and_filters(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    team = await _team(db_session)
    creator = await login_as(client, db_session, role="member", team_id=team.id)
    for key in ("k1", "k2"):
        assert (
            await client.post("/api/admin/bots", json=_bot_body(bot_key=key, name=f"名字 {key}"))
        ).status_code == 201
    other_team = await _team(db_session, "o")
    other = await login_as(client, db_session, role="member", team_id=other_team.id)
    assert (await client.post("/api/admin/bots", json=_bot_body(bot_key="k3"))).status_code == 201
    bots = {b.bot_key: b for b in (await db_session.execute(select(Bot))).scalars()}
    db_session.add(BotMember(bot_id=bots["k1"].id, user_id=other.id, added_by=creator.id))
    await db_session.commit()
    mine = (await client.get("/api/admin/bots")).json()["data"]
    assert sorted(b["bot_key"] for b in mine["items"]) == ["k1", "k3"] and mine["total"] == 2
    assert [
        b["bot_key"]
        for b in (await client.get("/api/admin/bots", params={"scope": "team"})).json()["data"][
            "items"
        ]
    ] == ["k3"]
    allb = (await client.get("/api/admin/bots", params={"scope": "all", "keyword": "名字"})).json()[
        "data"
    ]
    assert sorted(b["bot_key"] for b in allb["items"]) == ["k1", "k2"] and all(
        "system_prompt" not in b for b in allb["items"]
    )
    assert (
        await client.get("/api/admin/bots", params={"scope": "all", "enabled": "false"})
    ).json()["data"]["total"] == 0


async def test_patch_delete_and_locking(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    team = await _team(db_session)
    await login_as(client, db_session, role="member", team_id=team.id)
    created = (await client.post("/api/admin/bots", json=_bot_body())).json()["data"]
    bid, ver = created["id"], created["version"]
    assert (await client.patch(f"/api/admin/bots/{bid}", json={"name": "x"})).status_code == 428
    r = await client.patch(
        f"/api/admin/bots/{bid}",
        json={
            "name": "新名",
            "system_prompt": "新提示词-prompt-marker",
            "credentials": {"bot_id": "wecom-bot-id-1", "secret": "we••••ue"},
            "env_vars": {"DB_PASSWORD": "pw••••56", "NEW_KEY": "v1"},
        },
        headers={"If-Match": f'"{ver}"'},
    )
    assert r.status_code == 200, r.text
    out = r.json()["data"]
    assert (
        out["name"] == "新名"
        and out["version"] == ver + 1
        and set(out["env_vars"]) == {"DB_PASSWORD", "NEW_KEY"}
    )
    row = (await db_session.execute(select(Bot))).scalar_one()
    cipher = Cipher(base64.b64decode(MASTER_KEY))
    assert decrypt_json(cipher, row.credentials_enc, CREDENTIALS_AAD)["secret"] == (
        "wecom-secret-value"
    )
    assert decrypt_json(cipher, row.env_vars_enc, ENV_AAD) == {
        "DB_PASSWORD": "pw-123456",
        "NEW_KEY": "v1",
    }
    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "bot.update"))
    ).scalar_one()
    assert (
        audit.diff["name"] == ["销售助手", "新名"]
        and audit.diff["env_vars"] == ["***", "***"]
        and "credentials" not in audit.diff
    )
    assert audit.diff["system_prompt"] == ["***", "***"]
    assert "prompt-marker" not in str(audit.diff) and "你是销售" not in str(audit.diff)
    assert (
        await client.patch(
            f"/api/admin/bots/{bid}",
            json={"relay_server_id": None},
            headers={"If-Match": f'"{ver + 1}"'},
        )
    ).status_code == 409
    assert (
        await client.patch(
            f"/api/admin/bots/{bid}", json={"team_id": None}, headers={"If-Match": f'"{ver + 1}"'}
        )
    ).status_code == 403
    assert (
        await client.patch(
            f"/api/admin/bots/{bid}",
            json={"working_dir": "/data/../etc"},
            headers={"If-Match": f'"{ver + 1}"'},
        )
    ).status_code == 422
    assert (
        await client.patch(
            f"/api/admin/bots/{bid}", json={"name": "旧"}, headers={"If-Match": f'"{ver}"'}
        )
    ).status_code == 409
    await login_as(client, db_session, role="ai_committee")
    assert (
        await client.patch(
            f"/api/admin/bots/{bid}", json={"name": "x"}, headers={"If-Match": f'"{ver + 1}"'}
        )
    ).status_code == 403
    assert (await client.delete(f"/api/admin/bots/{bid}")).status_code == 403
    await login_as(client, db_session, role="platform_admin")
    assert (await client.delete(f"/api/admin/bots/{bid}")).status_code == 200
    removed = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "bot.delete"))
    ).scalar_one()
    assert removed.diff["system_prompt"] == ["***", "***"]
    assert "prompt-marker" not in str(removed.diff)
    assert (await client.get(f"/api/admin/bots/{bid}")).status_code == 404


def test_legacy_agent_timeout_is_not_exposed_or_written():
    from coreman.api.routers.bots import BotIn, BotPatch

    for schema in (BotIn, BotPatch):
        assert "agent_timeout_seconds" not in schema.model_json_schema()["properties"]
    assert BotPatch(agent_timeout_seconds=30).model_dump(exclude_unset=True) == {}


def test_deprecated_custom_command_modules_is_not_exposed_or_written():
    from coreman.api.routers.bots import BotIn, BotPatch

    for schema in (BotIn, BotPatch):
        assert "custom_command_modules" not in schema.model_json_schema()["properties"]
    assert BotPatch(custom_command_modules=["x"]).model_dump(exclude_unset=True) == {}

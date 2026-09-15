import base64

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.secrets import CREDENTIALS_AAD, encrypt_json
from coreman.core.crypto import Cipher
from coreman.core.db.models import AuditLog, Bot, ModelCatalog, RelayServer, User
from tests.api.conftest import MASTER_KEY, login_as, login_existing
from tests.api.test_bots import _bot_body, _team
from tests.fakes.runtime_node import attach_node


@pytest.fixture(autouse=True)
def _skip_memory_transfer(monkeypatch: pytest.MonkeyPatch) -> None:
    """这些用例只看切换规则；记忆迁移另有 test_memory_transfer 覆盖，这里不连节点。"""
    from coreman.core.bots import switch_relay

    async def not_configured(*args: object, **kwargs: object) -> str:
        return "not_configured"

    monkeypatch.setattr(switch_relay, "transfer", not_configured)


async def _setup(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> tuple[dict[str, object], dict[str, str], User]:
    team = await _team(db_session)
    relays = [
        RelayServer(name="claude01", model_provider="claude"),
        RelayServer(name="codex01", model_provider="codex"),
        RelayServer(
            name="teamb", model_provider="claude", team_id=(await _team(db_session, "b")).id
        ),
    ]
    db_session.add_all(relays)
    for relay in relays:
        await attach_node(db_session, relay)
    await db_session.commit()
    ids = {r.name: str(r.id) for r in (await db_session.execute(select(RelayServer))).scalars()}
    creator = await login_as(
        client, db_session, role="member", team_id=team.id, login_name="creator"
    )
    created = (
        await client.post("/api/admin/bots", json=_bot_body(relay_server_id=ids["claude01"]))
    ).json()["data"]
    return created, ids, creator


async def test_switch_relay_resolves_model(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, ids, _ = await _setup(client, db_session)
    r = await client.post(
        f"/api/admin/bots/{bot['id']}/switch-relay",
        json={"relay_server_id": ids["codex01"]},
        headers={"If-Match": f'"{bot["version"]}"'},
    )
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert (
        d["old_model"] == "claude-sonnet-4-6"
        and d["new_model"] == "codex/gpt-5.5"
        and d["bot"]["backend"] == "codex"
    )
    v = d["bot"]["version"]
    assert (
        await client.post(
            f"/api/admin/bots/{bot['id']}/switch-relay",
            json={"relay_server_id": ids["teamb"]},
            headers={"If-Match": f'"{v}"'},
        )
    ).status_code == 422
    r = await client.post(
        f"/api/admin/bots/{bot['id']}/switch-relay",
        json={"relay_server_id": ids["claude01"], "model": "claude-opus-4-6"},
        headers={"If-Match": f'"{v}"'},
    )
    assert r.status_code == 200 and r.json()["data"]["new_model"] == "claude-opus-4-6"
    audit = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.action == "bot.switch_relay").order_by(AuditLog.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(audit) == 2 and audit[0].diff["model"] == ["claude-sonnet-4-6", "codex/gpt-5.5"]


async def test_switch_relay_permissions_and_xhigh_downgrade(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, ids, _ = await _setup(client, db_session)
    row = (
        await db_session.execute(
            select(ModelCatalog).where(ModelCatalog.model == "claude-sonnet-4-6")
        )
    ).scalar_one()
    row.supports_xhigh = True
    await db_session.commit()
    v = bot["version"]
    r = await client.patch(
        f"/api/admin/bots/{bot['id']}",
        json={"effort_level": "xhigh"},
        headers={"If-Match": f'"{v}"'},
    )
    assert r.status_code == 200
    v = r.json()["data"]["version"]
    await login_as(client, db_session, role="member", team_id=None)
    assert (
        await client.post(
            f"/api/admin/bots/{bot['id']}/switch-relay",
            json={"relay_server_id": ids["codex01"]},
            headers={"If-Match": f'"{v}"'},
        )
    ).status_code == 403
    await login_as(client, db_session, role="ai_committee")
    r = await client.post(
        f"/api/admin/bots/{bot['id']}/switch-relay",
        json={"relay_server_id": ids["teamb"]},
        headers={"If-Match": f'"{v}"'},
    )
    # 同模型仍支持
    assert r.status_code == 200 and r.json()["data"]["bot"]["effort_level"] == "xhigh"
    v = r.json()["data"]["bot"]["version"]
    r = await client.post(
        f"/api/admin/bots/{bot['id']}/switch-relay",
        json={"relay_server_id": ids["codex01"]},
        headers={"If-Match": f'"{v}"'},
    )
    # 换模型后 xhigh 降级
    assert r.status_code == 200 and r.json()["data"]["bot"]["effort_level"] == "high"
    last = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.action == "bot.switch_relay").order_by(AuditLog.id)
            )
        )
        .scalars()
        .all()
    )[-1]
    assert last.diff["effort_level"] == ["xhigh", "high"]


async def test_switch_relay_checks_permission_before_if_match(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """权限先于乐观锁：无权的人漏带 If-Match 时是 403，不是「版本号没写对」的 428。"""
    bot, ids, creator = await _setup(client, db_session)
    await login_as(client, db_session, role="member", team_id=None)
    assert (
        await client.post(
            f"/api/admin/bots/{bot['id']}/switch-relay", json={"relay_server_id": ids["codex01"]}
        )
    ).status_code == 403
    # 有权的人漏带 If-Match 仍然是 428：这一步只调换顺序，没有放宽乐观锁。
    await login_existing(client, db_session, creator)
    assert (
        await client.post(
            f"/api/admin/bots/{bot['id']}/switch-relay", json={"relay_server_id": ids["codex01"]}
        )
    ).status_code == 428


async def test_same_relay_model_switch_skips_relay_visibility(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """visibility='admins' 的 relay 上，机器人管理员（member）在同一台上换模型走的也是这个
    入口：不该再要求 relay 可见（PATCH model 同样不看）；换到另一台隐藏 relay 仍要拦。"""
    team = await _team(db_session, "vis")
    hidden = RelayServer(name="hidden", model_provider="claude", visibility="admins")
    hidden2 = RelayServer(name="hidden2", model_provider="claude", visibility="admins")
    db_session.add_all([hidden, hidden2])
    await attach_node(db_session, hidden)
    await attach_node(db_session, hidden2)
    owner = await login_as(client, db_session, role="member", team_id=team.id, login_name="owner")
    bot = Bot(
        bot_key="hidden_bot",
        platform="wecom",
        name="隐藏机",
        created_by=owner.id,
        team_id=team.id,
        relay_server_id=hidden.id,
        model="claude-sonnet-4-6",
        working_dir="/d",
        # 创建者读详情会解密凭证，得是真密文
        credentials_enc=encrypt_json(
            Cipher(base64.b64decode(MASTER_KEY)), {"bot_id": "x", "secret": "y"}, CREDENTIALS_AAD
        ),
    )
    db_session.add(bot)
    await db_session.commit()
    await db_session.refresh(bot)
    r = await client.post(
        f"/api/admin/bots/{bot.id}/switch-relay",
        json={"relay_server_id": str(hidden.id), "model": "claude-opus-4-6"},
        headers={"If-Match": f'"{bot.version}"'},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["new_model"] == "claude-opus-4-6"
    moved = await client.post(
        f"/api/admin/bots/{bot.id}/switch-relay",
        json={"relay_server_id": str(hidden2.id)},
        headers={"If-Match": f'"{r.json()["data"]["bot"]["version"]}"'},
    )
    assert moved.status_code == 422


async def test_switch_relay_explicit_model_without_xhigh_is_422(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """显式带 model 时没有自动降档这回事：模型不支持 xhigh 就 422（页面对话框总是显式带模型）。"""
    bot, ids, _ = await _setup(client, db_session)
    row = (
        await db_session.execute(
            select(ModelCatalog).where(ModelCatalog.model == "claude-sonnet-4-6")
        )
    ).scalar_one()
    row.supports_xhigh = True
    await db_session.commit()
    r = await client.patch(
        f"/api/admin/bots/{bot['id']}",
        json={"effort_level": "xhigh"},
        headers={"If-Match": f'"{bot["version"]}"'},
    )
    assert r.status_code == 200, r.text
    bad = await client.post(
        f"/api/admin/bots/{bot['id']}/switch-relay",
        json={"relay_server_id": ids["claude01"], "model": "claude-opus-4-6"},
        headers={"If-Match": f'"{r.json()["data"]["version"]}"'},
    )
    assert bad.status_code == 422 and bad.json()["message"] == "该模型不支持 xhigh"


async def test_toggle_permissions(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    bot, _, creator = await _setup(client, db_session)
    r = await client.post(f"/api/admin/bots/{bot['id']}/toggle")
    assert r.status_code == 200 and r.json()["data"]["enabled"] is False
    await login_as(client, db_session, role="team_lead", team_id=creator.team_id)
    assert (await client.post(f"/api/admin/bots/{bot['id']}/toggle")).json()["data"][
        "enabled"
    ] is True
    await login_as(client, db_session, role="member", team_id=creator.team_id)
    assert (await client.post(f"/api/admin/bots/{bot['id']}/toggle")).status_code == 403


async def test_members_and_allowed_users(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _, creator = await _setup(client, db_session)
    colleague = User(login_name="lisi", display_name="李四")
    disabled = User(login_name="off", display_name="停用", status="disabled")
    db_session.add_all([colleague, disabled])
    await db_session.commit()
    r = await client.post(
        f"/api/admin/bots/{bot['id']}/members", json={"user_id": str(colleague.id)}
    )
    assert r.status_code == 201, r.text
    assert (
        await client.post(
            f"/api/admin/bots/{bot['id']}/members", json={"user_id": str(colleague.id)}
        )
    ).status_code == 409
    assert (
        await client.post(f"/api/admin/bots/{bot['id']}/members", json={"user_id": str(creator.id)})
    ).status_code == 409
    assert (
        await client.post(
            f"/api/admin/bots/{bot['id']}/members", json={"user_id": str(disabled.id)}
        )
    ).status_code == 422
    members = (await client.get(f"/api/admin/bots/{bot['id']}/members")).json()["data"]
    assert [m["login_name"] for m in members] == ["lisi"]
    assert (
        await client.put(
            f"/api/admin/bots/{bot['id']}/allowed-users", json={"user_ids": [str(disabled.id)]}
        )
    ).status_code == 422
    r = await client.put(
        f"/api/admin/bots/{bot['id']}/allowed-users",
        json={"user_ids": [str(colleague.id), str(creator.id)]},
    )
    assert r.status_code == 200 and len(r.json()["data"]) == 2
    listed = await client.get(f"/api/admin/bots/{bot['id']}/allowed-users")
    assert listed.status_code == 200 and listed.json()["data"] == r.json()["data"]
    # 协作者可改白名单但不能增删协作者
    lisi = (await db_session.execute(select(User).where(User.login_name == "lisi"))).scalar_one()
    await login_existing(client, db_session, lisi)
    assert (
        await client.put(f"/api/admin/bots/{bot['id']}/allowed-users", json={"user_ids": []})
    ).status_code == 200
    assert (
        await client.delete(f"/api/admin/bots/{bot['id']}/members/{colleague.id}")
    ).status_code == 403
    await login_as(client, db_session, role="platform_admin")
    assert (
        await client.delete(f"/api/admin/bots/{bot['id']}/members/{colleague.id}")
    ).status_code == 200
    assert (await client.get(f"/api/admin/bots/{bot['id']}/members")).json()["data"] == []
    actions = {a.action for a in (await db_session.execute(select(AuditLog))).scalars()}
    assert {"bot.member_add", "bot.member_remove", "bot.allowed_users"} <= actions

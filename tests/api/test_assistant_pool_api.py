"""助理池开关：只收能隔离运行、节点上没有普通员工、且与普通员工的节点不是同一个系统用户的实例。

普通员工也不能绑定或换到助理池的节点上。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine

from coreman.core.assistants import runtime as assistant_runtime
from coreman.core.db.models import RelayServer, RuntimeNode
from tests.api.conftest import login_as
from tests.api.test_assistant_runtime_protocol import beat
from tests.api.test_runtime_nodes import enrollment
from tests.fakes.runtime_node import add_node, add_node_relay
from tests.integration.assistant_helpers import seed_assistant, seed_standard

MACHINE = "cd" * 32
ASSISTANT_UID = 1500
STANDARD_UID = 1000


async def assistant_node(client, db_session, *, uid: int = ASSISTANT_UID, sandbox: bool = True):
    """装在这台机器上、以 `uid` 运行的一个节点，返回它的 claude 实例与节点凭证头。"""
    _, body, headers = await enrollment(client, db_session)
    await beat(
        client,
        headers,
        identity={"machine": MACHINE, "uid": uid},
        assistant_sandbox_v1=sandbox,
    )
    relay = await db_session.scalar(
        select(RelayServer).where(
            RelayServer.runtime_node_id == uuid.UUID(body["node_id"]),
            RelayServer.model_provider == "claude",
        )
    )
    assert relay is not None
    return relay, headers


async def standard_node(
    db_session,
    *,
    uid: int,
    machine: str = MACHINE,
    with_bot: bool = True,
    bot_enabled: bool = True,
):
    """同一台机器上另一个节点（普通员工的那个守护进程）。"""
    node = await add_node(db_session, name=f"standard-{uid}", machine_fingerprint=machine, uid=uid)
    relay = await add_node_relay(db_session, node=node)
    if with_bot:
        bot = await seed_standard(db_session)
        bot.relay_server_id = relay.id
        bot.enabled = bot_enabled
    await db_session.commit()
    return relay


async def enable(client, relay) -> tuple[int, str]:
    response = await client.put(
        f"/api/admin/relay-servers/{relay.id}/assistant-pool", json={"enabled": True}
    )
    return response.status_code, response.json().get("message", "")


async def test_assistant_node_next_to_standard_employees_under_another_user(client, db_session):
    await standard_node(db_session, uid=STANDARD_UID)
    relay, _ = await assistant_node(client, db_session)
    url = f"/api/admin/relay-servers/{relay.id}/assistant-pool"
    added = await client.put(url, json={"enabled": True})
    assert added.status_code == 200, added.text
    assert added.json()["data"]["assistant_pool"] is True
    assert added.json()["data"]["assistant_sandbox"] is True
    removed = await client.put(url, json={"enabled": False})
    assert removed.json()["data"]["assistant_pool"] is False


async def test_same_system_user_as_a_standard_node_is_refused(client, db_session):
    await standard_node(db_session, uid=ASSISTANT_UID)
    relay, _ = await assistant_node(client, db_session)
    status, message = await enable(client, relay)
    assert status == 422 and "单独的系统用户" in message


async def test_same_user_sibling_must_itself_be_a_pool_node(client, db_session):
    idle = await standard_node(db_session, uid=ASSISTANT_UID, with_bot=False)
    relay, _ = await assistant_node(client, db_session)
    assert (await enable(client, relay))[0] == 422
    idle.assistant_pool = True
    await db_session.commit()
    assert (await enable(client, relay))[0] == 200


async def test_another_machine_with_the_same_uid_does_not_matter(client, db_session):
    await standard_node(db_session, uid=ASSISTANT_UID, machine="ef" * 32)
    relay, _ = await assistant_node(client, db_session)
    assert (await enable(client, relay))[0] == 200


@pytest.mark.parametrize(
    ("setup", "words"),
    [
        ("no_sandbox", "隔离运行"),
        ("no_identity", "本机指纹"),
        ("root", "root"),
        ("private", "私有"),
    ],
)
async def test_refusals(client, db_session, setup, words):
    relay, headers = await assistant_node(
        client,
        db_session,
        uid=0 if setup == "root" else ASSISTANT_UID,
        sandbox=setup != "no_sandbox",
    )
    if setup == "no_identity":
        # 无身份的心跳：节点之前上报的本机指纹与 uid 被清掉，没有新的可补。
        node = await db_session.get(RuntimeNode, relay.runtime_node_id)
        node.machine_fingerprint, node.uid = None, None
        await db_session.commit()
    if setup == "private":
        relay.visibility = "private"
        await db_session.commit()
    status, message = await enable(client, relay)
    assert status == 422 and words in message
    await db_session.refresh(relay)
    assert relay.assistant_pool is False


@pytest.mark.parametrize("enabled", [True, False])
async def test_pool_nodes_must_not_host_standard_bots(client, db_session, enabled):
    """节点上有普通员工就不能入池，员工停用了也一样（之后还能再启用）。"""
    relay, _ = await assistant_node(client, db_session)
    bot = await seed_standard(db_session)
    bot.relay_server_id = relay.id
    bot.enabled = enabled
    await db_session.commit()
    status, message = await enable(client, relay)
    assert status == 422 and "普通员工" in message


async def test_a_disabled_standard_employee_on_a_sibling_node_blocks_too(client, db_session):
    sibling = await standard_node(db_session, uid=ASSISTANT_UID, bot_enabled=False)
    sibling.assistant_pool = True
    await db_session.commit()
    relay, _ = await assistant_node(client, db_session)
    status, message = await enable(client, relay)
    assert status == 422 and "单独的系统用户" in message


async def test_enabling_takes_the_node_lock_before_the_checks(client, db_session):
    """入池与绑定普通员工按节点串行：先对被加入的那个节点取咨询锁，且只取这一把。"""
    await standard_node(db_session, uid=STANDARD_UID)
    relay, _ = await assistant_node(client, db_session)
    seen: list[tuple[str, str]] = []

    def record(conn, cursor, statement, parameters, *args):
        seen.append((statement, str(parameters)))

    # 接口用的是应用自己的引擎，不是 db_session 的，所以挂在 Engine 类上。
    event.listen(Engine, "before_cursor_execute", record)
    try:
        assert (await enable(client, relay))[0] == 200
    finally:
        event.remove(Engine, "before_cursor_execute", record)
    locks = [i for i, (s, _) in enumerate(seen) if "pg_advisory_xact_lock" in s]
    assert len(locks) == 1
    assert str(relay.runtime_node_id) in seen[locks[0]][1]
    writes = [
        i for i, (s, _) in enumerate(seen) if s.startswith(("UPDATE relay_servers", "INSERT"))
    ]
    checks = [i for i, (s, _) in enumerate(seen) if "FROM bots" in s]
    assert writes and checks
    assert locks[0] < min(writes) and locks[0] < min(checks)


async def test_enabling_uses_the_same_node_lock_as_the_standard_bind(
    client, db_session, monkeypatch
):
    """入池与 `standard_bind_error` 走同一个 `lock_node`、同一个键，互相等待。"""
    relay, _ = await assistant_node(client, db_session)
    keys: list[str] = []
    original = assistant_runtime.lock_node

    async def spy(session, node_id):
        keys.append(f"assistant-pool-node:{node_id}")
        await original(session, node_id)

    monkeypatch.setattr(assistant_runtime, "lock_node", spy)
    assert (await enable(client, relay))[0] == 200
    assert keys == [f"assistant-pool-node:{relay.runtime_node_id}"]
    assert await assistant_runtime.standard_bind_error(db_session, relay) == (
        assistant_runtime.POOL_NODE_MESSAGE
    )


async def test_only_admins_may_toggle_the_pool(client, db_session):
    relay, _ = await assistant_node(client, db_session)
    url = f"/api/admin/relay-servers/{relay.id}/assistant-pool"
    member = await login_as(client, db_session, role="member")
    relay.owner_id = member.id
    await db_session.commit()
    # 实例所有者本人（普通成员）也不行。
    assert (await client.put(url, json={"enabled": True})).status_code == 403
    await login_as(client, db_session, role="team_lead")
    assert (await client.put(url, json={"enabled": True})).status_code == 403
    await db_session.refresh(relay)
    assert relay.assistant_pool is False
    await login_as(client, db_session, role="ai_committee")
    assert (await client.put(url, json={"enabled": True})).status_code == 200
    await login_as(client, db_session, role="platform_admin")
    assert (await client.put(url, json={"enabled": False})).status_code == 200


async def test_unknown_relay_is_404(client, db_session):
    await login_as(client, db_session, role="platform_admin")
    response = await client.put(
        f"/api/admin/relay-servers/{uuid.uuid4()}/assistant-pool", json={"enabled": True}
    )
    assert response.status_code == 404


async def test_turning_the_pool_off_leaves_bound_assistants_paused_not_migrated(client, db_session):
    """移出池不改绑助理：它们仍绑在这个实例上，但不再合格，下一轮按「不在池里」暂停并通知所有者。"""
    relay, _ = await assistant_node(client, db_session)
    assert (await enable(client, relay))[0] == 200
    await db_session.refresh(relay)
    bot, _ = await seed_assistant(db_session)
    bot.relay_server_id = relay.id
    await db_session.commit()
    assert await assistant_runtime.turn_blocker(db_session, bot) != "runtime_not_allowed"
    off = await client.put(
        f"/api/admin/relay-servers/{relay.id}/assistant-pool", json={"enabled": False}
    )
    assert off.status_code == 200
    await db_session.refresh(relay)
    await db_session.refresh(bot)
    assert bot.relay_server_id == relay.id and bot.enabled is True
    assert await assistant_runtime.turn_blocker(db_session, bot) == "runtime_not_allowed"
    assert bot.kind == "assistant"

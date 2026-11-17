"""令牌交付方式：切到平台代理前的校验，以及「系统 × 用得到它的 AI 员工」盘点。"""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import (
    Bot,
    BotSystemGrant,
    BusinessSystem,
    RuntimeNode,
    SystemCatalog,
)
from coreman.core.systems_catalog import policy as catalog_policy
from tests.api.conftest import login_as
from tests.api.test_admin_cli import add_user, cli  # noqa: F401
from tests.fakes.business_system import BASE_URL, SPEC_URL, FakeBusinessSystem, install
from tests.integration.assistant_helpers import into_assistant
from tests.integration.worker_helpers import seed_bot

SYSTEMS = "/api/admin/systems"
DELIVERY = "/api/admin/system-delivery"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeBusinessSystem:
    system = FakeBusinessSystem()
    install(monkeypatch, system)
    return system


async def put(client: httpx.AsyncClient, key: str, body: dict[str, object]) -> httpx.Response:
    rows = (await client.get(SYSTEMS)).json()["data"]["items"]
    version = next(row["version"] for row in rows if row["key"] == key)
    return await client.put(f"{SYSTEMS}/{key}", json=body, headers={"If-Match": str(version)})


async def test_switching_to_proxy_needs_an_openapi_url_and_a_usable_catalog(
    client: httpx.AsyncClient, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    await login_as(client, db_session, role="platform_admin")
    body: dict[str, object] = {"name": "库存", "base_url": BASE_URL, "allowed_bot_ids": []}
    # 新建就是平台代理：没有 OpenAPI 地址直接拒绝，并说明该怎么做。
    r = await client.post(SYSTEMS, json={**body, "key": "stock", "token_delivery": "proxy"})
    assert r.status_code == 422 and "OpenAPI 地址" in r.json()["message"]
    assert (await client.post(SYSTEMS, json={**body, "key": "stock"})).status_code == 201
    r = await put(client, "stock", {**body, "token_delivery": "proxy"})
    assert r.status_code == 422 and "OpenAPI 地址" in r.json()["message"]
    row = await db_session.get(BusinessSystem, "stock", populate_existing=True)
    assert row is not None and row.token_delivery == "env"

    # 填上地址后目录拉取成功；之后目录坏了（没有可用的编译结果），切过去就是「无法调用」。
    body["openapi_url"] = SPEC_URL
    r = await put(client, "stock", body)
    assert r.status_code == 200 and r.json()["data"]["catalog"]["status"] == "ok"
    catalog = await db_session.get(SystemCatalog, "stock", populate_existing=True)
    assert catalog is not None
    catalog.status, catalog.error, catalog.compiled = "error", "http_500", None
    await db_session.commit()
    r = await put(client, "stock", {**body, "token_delivery": "proxy"})
    assert r.status_code == 409
    assert "http_500" in r.json()["message"] and "systems refresh stock" in r.json()["message"]
    # 还没拉完的目录不算坏。
    catalog.error = "pending"
    await db_session.commit()
    r = await put(client, "stock", {**body, "token_delivery": "proxy"})
    assert r.status_code == 200 and r.json()["data"]["token_delivery"] == "proxy"

    # 代理系统上清掉 OpenAPI 地址同样会变成「无法调用」；改回 env 不设门槛。
    r = await put(client, "stock", {**body, "openapi_url": None, "token_delivery": "proxy"})
    assert r.status_code == 422
    r = await put(client, "stock", {**body, "openapi_url": None, "token_delivery": "env"})
    assert r.status_code == 200 and r.json()["data"]["token_delivery"] == "env"


async def test_proxy_switch_with_a_new_url_and_legacy_rows_are_not_blocked(
    client: httpx.AsyncClient, db_session: AsyncSession, fake: FakeBusinessSystem
) -> None:
    await login_as(client, db_session, role="platform_admin")
    db_session.add_all(
        [
            # 校验上线前就存在的、没有 OpenAPI 地址的代理系统：还能照常改别的字段。
            BusinessSystem(key="legacy", name="旧系统", token_delivery="proxy", allowed_bot_ids=[]),
            BusinessSystem(
                key="stock",
                name="库存",
                base_url=BASE_URL,
                openapi_url=BASE_URL + "/old.yaml",
                allowed_bot_ids=[],
            ),
            SystemCatalog(
                system_key="stock",
                spec_url=BASE_URL + "/old.yaml",
                status="error",
                error="http_404",
            ),
        ]
    )
    await db_session.commit()
    r = await put(
        client, "legacy", {"name": "旧系统 2", "token_delivery": "proxy", "allowed_bot_ids": []}
    )
    assert r.status_code == 200 and r.json()["data"]["name"] == "旧系统 2"
    # 同一次保存里换了地址：旧目录的状态说明不了什么，保存后按新地址重新拉取。
    r = await put(
        client,
        "stock",
        {
            "name": "库存",
            "base_url": BASE_URL,
            "openapi_url": SPEC_URL,
            "token_delivery": "proxy",
            "allowed_bot_ids": [],
        },
    )
    assert r.status_code == 200 and r.json()["data"]["catalog"]["status"] == "ok"


async def seed_inventory(db_session: AsyncSession) -> Bot:
    """一个在节点上的员工、一个没分配运行时的员工、一个停用的员工，和四种系统。"""
    bot, _, _ = await seed_bot(db_session)
    common = {
        "platform": "wecom",
        "created_by": bot.created_by,
        "model": bot.model,
        "system_prompt": "",
        "merged_system_prompt": "",
        "credentials_enc": bot.credentials_enc,
        "env_vars_enc": bot.env_vars_enc,
    }
    db_session.add_all(
        [
            Bot(
                bot_key="demo_bot",
                name="演示",
                working_dir="/data/skills/demo_bot",
                unprompted_system_tokens=True,
                **common,
            ),
            Bot(
                bot_key="idle_bot",
                name="停用",
                working_dir="/data/skills/idle_bot",
                enabled=False,
                relay_server_id=bot.relay_server_id,
                **common,
            ),
            BusinessSystem(
                key="stock", name="库存", default_for_all_bots=True, allowed_bot_ids=None
            ),
            BusinessSystem(
                key="orders",
                name="订单",
                token_delivery="proxy",
                base_url="https://orders.example.com",
                openapi_url="https://orders.example.com/openapi.json",
                allowed_bot_ids=[bot.id],
                sort_order=1,
            ),
            # 默认授权但白名单为空、已停用的系统：每轮都签不出来，盘点里也不出现。
            BusinessSystem(
                key="lookup",
                name="检索",
                token_delivery="mcp",
                mcp_url="https://mcp.example.com/mcp",
                default_for_all_bots=True,
                allowed_bot_ids=[],
            ),
            BusinessSystem(
                key="retired",
                name="下线",
                default_for_all_bots=True,
                allowed_bot_ids=None,
                enabled=False,
            ),
        ]
    )
    await db_session.flush()
    db_session.add(BotSystemGrant(bot_id=bot.id, system_key="orders", allow_write=True))
    await db_session.commit()
    return bot


async def test_delivery_inventory_lists_systems_by_the_bots_that_can_use_them(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot = await seed_inventory(db_session)
    await login_as(client, db_session, role="member")
    assert (await client.get(DELIVERY)).status_code == 403
    await login_as(client, db_session, role="ai_committee")
    r = await client.get(DELIVERY)
    assert r.status_code == 200, r.text
    rows = r.json()["data"]
    assert [(row["system_key"], row["bot_key"]) for row in rows] == [
        ("stock", "demo_bot"),
        ("stock", "sales_bot"),
        ("orders", "sales_bot"),
    ]
    assert rows[0] == {
        "system_key": "stock",
        "system_name": "库存",
        "token_delivery": "env",
        "bot_id": rows[0]["bot_id"],
        "bot_key": "demo_bot",
        "bot_name": "演示",
        "source": "default",
        "allow_write": False,
        "runtime_node": None,
        "catalog_runtime": False,
        "unprompted_system_tokens": True,
    }
    assert rows[1]["runtime_node"] == "node-1" and not rows[1]["unprompted_system_tokens"]
    proxied = rows[2]
    assert proxied["bot_id"] == str(bot.id) and proxied["token_delivery"] == "proxy"
    assert proxied["source"] == "grant" and proxied["allow_write"] is True
    # 节点还没声明目录能力：切成平台代理的系统在这位员工那里用不了。
    assert proxied["catalog_runtime"] is False
    node = await db_session.scalar(select(RuntimeNode))
    assert node is not None
    node.capabilities = {"claude": {catalog_policy.RUNTIME_CAPABILITY: True}}
    await db_session.commit()
    rows = (await client.get(DELIVERY, params={"mode": "proxy"})).json()["data"]
    assert [(row["system_key"], row["catalog_runtime"]) for row in rows] == [("orders", True)]
    assert (await client.get(DELIVERY, params={"mode": "mcp"})).json()["data"] == []
    assert (await client.get(DELIVERY, params={"mode": "bogus"})).status_code == 422


async def test_delivery_inventory_hides_other_peoples_assistants(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot = await seed_inventory(db_session)
    bot.platform = "feishu"
    owner = await login_as(client, db_session, role="platform_admin")
    await into_assistant(db_session, bot, owner)
    rows = (await client.get(DELIVERY)).json()["data"]
    assert ("stock", "sales_bot") in [(row["system_key"], row["bot_key"]) for row in rows]
    # 助理记录只对所有者可见：别的管理员盘点时看不到它。
    await login_as(client, db_session, role="platform_admin")
    rows = (await client.get(DELIVERY)).json()["data"]
    assert [(row["system_key"], row["bot_key"]) for row in rows] == [("stock", "demo_bot")]


async def test_systems_delivery_command(cli, db_session: AsyncSession) -> None:  # noqa: F811
    bot = await seed_inventory(db_session)
    await add_user(db_session, "ops", "platform_admin")
    await add_user(db_session, "bob")
    code, _, err = await cli("systems", "delivery", "--as", "bob")
    assert code == 1 and "没有权限" in err
    code, out, err = await cli("systems", "delivery", "--as", "ops")
    assert code == 0, err
    lines = out.splitlines()
    assert "令牌下发" in lines[0] and "没人点名也下发" in lines[0]
    assert len(lines) == 4
    assert "stock（库存）" in lines[1] and "demo_bot（演示）" in lines[1]
    assert "默认全员" in lines[1]
    # 允许写入只对平台代理的系统有意义：别的方式显示为空。
    assert "orders（订单）" in lines[3] and "显式授权" in lines[3] and "node-1" in lines[3]
    code, out, err = await cli("systems", "delivery", "--mode", "proxy", "--json", "--as", "ops")
    assert code == 0, err
    rows = json.loads(out)
    assert [(r["system_key"], r["bot_id"], r["allow_write"]) for r in rows] == [
        ("orders", str(bot.id), True)
    ]
    code, out, _ = await cli("systems", "delivery", "--mode", "mcp", "--as", "ops")
    assert code == 0 and "没有 AI 员工用得到" in out

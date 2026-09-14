"""公告管理 API（spec §5.5、§10.2：只有 ai_committee / platform_admin 能看能改）。"""

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import AuditLog, Bot, RelayServer, User
from tests.api.conftest import login_as

BASE = "/api/admin/announcements"


async def _fixtures(session: AsyncSession, creator: User) -> tuple[RelayServer, Bot]:
    relay = RelayServer(name="r-ann", host="ann.test", clawrelay_port=80, model_provider="claude")
    session.add(relay)
    await session.flush()
    bot = Bot(
        bot_key="ann_bot",
        platform="wecom",
        name="公告机器人",
        created_by=creator.id,
        relay_server_id=relay.id,
        model="vllm/claude-sonnet-4-6",
        working_dir="/d",
        credentials_enc="enc:v1:x",
    )
    session.add(bot)
    await session.commit()
    return relay, bot


async def test_roles_and_crud(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    await login_as(client, db_session, role="team_lead")
    assert (await client.get(BASE)).status_code == 403
    assert (await client.post(BASE, json={"scope": "global", "content": "x"})).status_code == 403
    actor = await login_as(client, db_session, role="ai_committee")
    relay, bot = await _fixtures(db_session, actor)
    created = await client.post(
        BASE,
        json={"scope": "bot", "bot_id": str(bot.id), "content": "维护中", "is_active": True},
    )
    assert created.status_code == 201, created.text
    row = created.json()["data"]
    assert row["scope"] == "bot"
    assert row["bot_key"] == "ann_bot"
    assert row["bot_name"] == "公告机器人"
    assert row["time_status"] == "active"
    assert row["created_by"] == str(actor.id)
    # 写端点同样只认管理角色：PUT / toggle / DELETE 越权都得 403，且行不能被改动。
    await login_as(client, db_session, role="member")
    forbidden_put = await client.put(
        f"{BASE}/{row['id']}",
        json={"scope": "global", "content": "越权改写", "is_active": False},
    )
    assert forbidden_put.status_code == 403, forbidden_put.text
    assert (await client.post(f"{BASE}/{row['id']}/toggle")).status_code == 403
    assert (await client.delete(f"{BASE}/{row['id']}")).status_code == 403
    await login_as(client, db_session, role="ai_committee")
    intact = (await client.get(BASE)).json()["data"]
    assert [(a["id"], a["content"], a["is_active"]) for a in intact] == [
        (row["id"], "维护中", True)
    ]
    later = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    upd = await client.put(
        f"{BASE}/{row['id']}",
        json={
            "scope": "relay",
            "relay_server_id": str(relay.id),
            "content": "换成按 relay",
            "is_active": True,
            "start_at": later,
        },
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["data"]["relay_name"] == "r-ann"
    assert upd.json()["data"]["time_status"] == "pending"
    tog = await client.post(f"{BASE}/{row['id']}/toggle")
    assert tog.status_code == 200 and tog.json()["data"]["is_active"] is False
    lst = (await client.get(BASE)).json()["data"]
    assert [a["id"] for a in lst] == [row["id"]]
    assert (await client.delete(f"{BASE}/{row['id']}")).status_code == 200
    assert (await client.get(BASE)).json()["data"] == []
    actions = (
        (
            await db_session.execute(
                select(AuditLog.action)
                .where(AuditLog.target_type == "announcement")
                .order_by(AuditLog.id)
            )
        )
        .scalars()
        .all()
    )
    assert actions == [
        "announcement.create",
        "announcement.update",
        "announcement.toggle",
        "announcement.delete",
    ]


async def test_validation(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    actor = await login_as(client, db_session, role="platform_admin")
    relay, bot = await _fixtures(db_session, actor)
    assert (await client.post(BASE, json={"scope": "bot", "content": "缺目标"})).status_code == 422
    assert (
        await client.post(
            BASE, json={"scope": "global", "bot_id": str(bot.id), "content": "全局带目标"}
        )
    ).status_code == 422
    assert (
        await client.post(
            BASE,
            json={
                "scope": "relay",
                "relay_server_id": str(relay.id),
                "bot_id": str(bot.id),
                "content": "两个目标",
            },
        )
    ).status_code == 422
    assert (await client.post(BASE, json={"scope": "global", "content": ""})).status_code == 422
    now = datetime.now(UTC)
    bad = await client.post(
        BASE,
        json={
            "scope": "global",
            "content": "倒着的窗口",
            "start_at": now.isoformat(),
            "end_at": (now - timedelta(minutes=1)).isoformat(),
        },
    )
    assert bad.status_code == 422
    missing = await client.post(
        BASE,
        json={
            "scope": "bot",
            "bot_id": "00000000-0000-0000-0000-000000000001",
            "content": "不存在的 bot",
        },
    )
    assert missing.status_code == 422
    expired = await client.post(
        BASE,
        json={
            "scope": "global",
            "content": "过去的",
            "end_at": (now - timedelta(minutes=1)).isoformat(),
        },
    )
    assert expired.status_code == 201, expired.text
    assert expired.json()["data"]["time_status"] == "expired"
    assert (await client.get(f"{BASE}/00000000-0000-0000-0000-000000000009")).status_code in (
        404,
        405,
    )


async def test_naive_window_is_utc(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    """`datetime-local` 这类选择器给的是不带时区的串：按 UTC 解释后入库，不能直接甩给库。"""
    await login_as(client, db_session, role="ai_committee")
    now = datetime.now(UTC).replace(microsecond=0)
    start_naive = (now - timedelta(hours=1)).replace(tzinfo=None)
    end_naive = (now + timedelta(hours=1)).replace(tzinfo=None)
    created = await client.post(
        BASE,
        json={
            "scope": "global",
            "content": "不带时区的窗口",
            "start_at": start_naive.isoformat(),
            "end_at": end_naive.isoformat(),
        },
    )
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert datetime.fromisoformat(data["start_at"]) == start_naive.replace(tzinfo=UTC)
    assert datetime.fromisoformat(data["end_at"]) == end_naive.replace(tzinfo=UTC)
    assert data["time_status"] == "active"
    pending = await client.post(
        BASE,
        json={
            "scope": "global",
            "content": "还没到点",
            "start_at": (now + timedelta(hours=1)).replace(tzinfo=None).isoformat(),
        },
    )
    assert pending.status_code == 201, pending.text
    assert pending.json()["data"]["time_status"] == "pending"
    # 一头不带时区一头带：先归一再比较，不能炸成 TypeError，要落成干净的 422。
    crossed = await client.post(
        BASE,
        json={
            "scope": "global",
            "content": "倒着的混合窗口",
            "start_at": (now + timedelta(hours=2)).replace(tzinfo=None).isoformat(),
            "end_at": (now + timedelta(hours=1)).isoformat(),
        },
    )
    assert crossed.status_code == 422, crossed.text

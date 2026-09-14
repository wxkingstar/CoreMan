from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.audit import record_audit
from tests.api.conftest import login_as


async def test_filters_and_roles(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    for i, action in enumerate(["bot.create", "bot.update", "team.create"]):
        await record_audit(
            db_session,
            action=action,
            actor_login="zhangsan",
            target_type="bot" if action.startswith("bot") else "team",
            target_id=f"t{i}",
            diff={"k": [i, i + 1]},
            ip="10.0.0.1",
        )
    await record_audit(
        db_session,
        action="bot.delete",
        actor_login="lisi",
        target_type="bot",
        target_id="t9",
        ip="bad-ip",
    )
    await db_session.commit()
    await login_as(client, db_session, role="member")
    assert (await client.get("/api/admin/audit-logs")).status_code == 403
    await login_as(client, db_session, role="ai_committee")
    r = await client.get("/api/admin/audit-logs", params={"action": "bot.", "actor": "zhang"})
    assert r.status_code == 200
    items = r.json()["data"]["items"]
    assert (
        [x["action"] for x in items] == ["bot.update", "bot.create"]
        and items[0]["diff"] == {"k": [1, 2]}
        and items[0]["ip"] == "10.0.0.1"
    )
    assert (await client.get("/api/admin/audit-logs", params={"target_type": "team"})).json()[
        "data"
    ]["total"] == 1
    assert (await client.get("/api/admin/audit-logs", params={"actor": "li%"})).json()["data"][
        "total"
    ] == 0  # % 被转义
    all_items = (await client.get("/api/admin/audit-logs")).json()["data"]
    # 上面一共写了 4 条；不带过滤时全都在，且按 id 倒序
    assert all_items["total"] >= 4 and all_items["items"][0]["id"] > all_items["items"][1]["id"]


async def test_time_range_filters(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    await record_audit(
        db_session, action="bot.create", actor_login="zhangsan", target_type="bot", target_id="t1"
    )
    await db_session.commit()
    await login_as(client, db_session, role="platform_admin")

    async def total(**params: str) -> int:
        r = await client.get("/api/admin/audit-logs", params=params)
        assert r.status_code == 200, r.text
        return int(r.json()["data"]["total"])

    created = (await client.get("/api/admin/audit-logs")).json()["data"]["items"][0]["created_at"]
    at = datetime.fromisoformat(created)
    assert await total(since=at.isoformat()) == 1
    assert await total(since=(at + timedelta(seconds=1)).isoformat()) == 0
    # 不带时区的入参（日期选择器常见）按 UTC 解释，不能因此 500
    naive = at.astimezone(UTC).replace(tzinfo=None)
    assert await total(until=naive.isoformat()) == 1
    assert await total(until=(naive - timedelta(seconds=1)).isoformat()) == 0

"""「我的助理」页的访问记录：每个读密文的接口各留下说好的那几行，行里没有任何内容。

新增一条 GET 路由而不登记进 `READS`，第一个用例就失败：先想清楚它解不解密、该记什么。
"""

from __future__ import annotations

import uuid
from datetime import date

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import access_log, followups, notes
from coreman.core.db.models import Bot
from tests.api.conftest import login_as
from tests.api.my_assistant_helpers import URL, code, owned
from tests.api.test_csrf import _api_routes
from tests.integration.assistant_helpers import SECRET

pytestmark = pytest.mark.usefixtures("assistant_master")
PAGE = "规则/口吻"
# GET 路由（去掉前缀）→ 调一次应当留下的（动作, 对象）。
READS: dict[str, list[tuple[str, str]]] = {
    # 概览只有元数据：状态、运行时、轮次、轮询健康。
    "": [],
    "/attention": [
        ("approvals.read", "pending"),
        ("jobs.read", "blocked"),
        ("followups.read", "today"),
    ],
    "/jobs": [("jobs.read", "list")],
    "/followups": [("followups.read", "open")],
    "/approvals/recent": [("approvals.read", "recent")],
    "/notes": [("notes.index", "")],
    "/settings": [("settings.read", "")],
    # 访问记录本身只有编号、动作与时间：看它不解密任何东西。
    "/access-log": [],
}
# 不是 GET 却把解密后的内容带回来的：知识页的路径只放请求体；改设置、即时上下班的响应里
# 带着解密后的重点群与忽略名单。键是（方法, 路径）。
ECHOES: dict[tuple[str, str], tuple[dict[str, str], list[tuple[str, str]]]] = {
    ("POST", "/notes/read"): ({"path": PAGE}, [("notes.read", "page")]),
    ("PATCH", "/settings"): ({"timezone": "UTC"}, [("settings.read", "update")]),
    ("POST", "/shift"): ({"state": "on"}, [("settings.read", "shift")]),
}
Call = tuple[str, str]


async def _read(client: httpx.AsyncClient, call: Call) -> httpx.Response:
    method, path = call
    body = ECHOES[call][0] if call in ECHOES else None
    return await client.request(method, f"{URL}{path}", json=body)


def _expected() -> dict[Call, list[tuple[str, str]]]:
    reads: dict[Call, list[tuple[str, str]]] = {("GET", path): rows for path, rows in READS.items()}
    return {**reads, **{call: rows for call, (_, rows) in ECHOES.items()}}


async def _stored(db_session: AsyncSession) -> list[tuple[str, str, str, uuid.UUID | None]]:
    rows = await db_session.execute(
        text("SELECT action, target, actor_kind, actor_id FROM assistant_access_log ORDER BY id")
    )
    return [(row[0], row[1], row[2], row[3]) for row in rows.all()]


async def _seed(db_session: AsyncSession, bot: Bot) -> None:
    """一页知识页、一条今天到期的待跟进；路径、标题、摘要、正文里都带着机密片段。"""
    await notes.write(
        db_session,
        bot.id,
        PAGE,
        body=f"回复要短。{SECRET}",
        summary=f"口吻的规矩 {SECRET}",
        title=f"口吻 {SECRET}",
    )
    await followups.add(
        db_session, bot.id, category="remind", title=f"续约 {SECRET}", due_on=date(2000, 1, 1)
    )
    await db_session.commit()


def test_every_get_route_under_the_prefix_is_in_the_table(app: FastAPI) -> None:
    found = {
        route.path.removeprefix(URL)
        for route in _api_routes(app.routes)
        if route.path.startswith(URL) and "GET" in route.methods
    }
    assert found == set(READS)


async def test_each_read_leaves_exactly_its_records(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await owned(client, db_session)
    await _seed(db_session, bot)
    for call, expected in _expected().items():
        path = " ".join(call)
        before = len(await _stored(db_session))
        response = await _read(client, call)
        assert response.status_code == 200, (path, response.text)
        added = (await _stored(db_session))[before:]
        assert [(action, target) for action, target, _, _ in added] == expected, path
        assert all(kind == "owner" and actor == owner.id for _, _, kind, actor in added), path


async def test_the_log_holds_no_content(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    await _seed(db_session, bot)
    for call in _expected():
        assert (await _read(client, call)).status_code == 200, call
    logged = " ".join(f"{action} {target}" for action, target, _, _ in await _stored(db_session))
    assert logged
    # 正文、摘要、标题里的机密片段，路径的两段，待跟进的标题：一样都不在。
    for piece in (SECRET, "规则", "口吻", "续约"):
        assert piece not in logged


LOG = f"{URL}/access-log"


async def test_the_owner_pages_through_their_own_records(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    await _seed(db_session, bot)
    for _ in range(3):
        await client.get(f"{URL}/notes")
    await access_log.record(db_session, bot.id, None, "cli.rewrap")
    await db_session.commit()
    first = await client.get(LOG, params={"limit": 2})
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    data = first.json()["data"]
    assert [(item["action"], item["actor_kind"], item["actor_name"]) for item in data["items"]] == [
        ("cli.rewrap", "system", None),
        ("notes.index", "owner", "张三"),
    ]
    assert set(data["items"][0]) == {"id", "at", "actor_kind", "actor_name", "action", "target"}
    assert data["next_before"] == data["items"][-1]["id"]
    rest = (await client.get(LOG, params={"before": data["next_before"]})).json()["data"]
    assert [item["action"] for item in rest["items"]] == ["notes.index", "notes.index"]
    assert rest["next_before"] is None
    # 看访问记录本身不算一次解密：还是那四行。
    assert len(await _stored(db_session)) == 4


async def test_nobody_reads_someone_elses_records(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    await _seed(db_session, bot)
    await client.get(f"{URL}/notes")
    # 另一位有自己助理的平台管理员：只看得到他自己的（空的）。
    await owned(client, db_session, role="platform_admin")
    mine_only = await client.get(LOG)
    assert mine_only.json()["data"] == {"items": [], "next_before": None}
    assert str(bot.id) not in mine_only.text
    # 没有助理的平台管理员：没有助理，也就没有记录可看。
    await login_as(client, db_session, role="platform_admin")
    refused = await client.get(LOG)
    assert refused.status_code == 404 and code(refused) == "no_assistant"


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"before": 0}, {"before": "x"}])
async def test_bad_paging_is_refused(
    client: httpx.AsyncClient, db_session: AsyncSession, params: dict[str, object]
) -> None:
    await owned(client, db_session)
    assert (await client.get(LOG, params=params)).status_code == 422

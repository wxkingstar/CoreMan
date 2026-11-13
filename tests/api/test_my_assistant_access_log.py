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

from coreman.core.assistants import followups, notes
from coreman.core.db.models import Bot
from tests.api.my_assistant_helpers import URL, owned
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
}
# 用 POST 读的：知识页的路径只放请求体；即时上下班的响应里带着解密后的设置。
POSTED: dict[str, tuple[dict[str, str], list[tuple[str, str]]]] = {
    "/notes/read": ({"path": PAGE}, [("notes.read", "page")]),
    "/shift": ({"state": "on"}, [("settings.read", "shift")]),
}


async def _read(client: httpx.AsyncClient, path: str) -> httpx.Response:
    if path in POSTED:
        return await client.post(f"{URL}{path}", json=POSTED[path][0])
    return await client.get(f"{URL}{path}")


def _expected() -> dict[str, list[tuple[str, str]]]:
    return {**READS, **{path: rows for path, (_, rows) in POSTED.items()}}


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
    for path, expected in _expected().items():
        before = len(await _stored(db_session))
        response = await _read(client, path)
        assert response.status_code == 200, (path, response.text)
        added = (await _stored(db_session))[before:]
        assert [(action, target) for action, target, _, _ in added] == expected, path
        assert all(kind == "owner" and actor == owner.id for _, _, kind, actor in added), path


async def test_the_log_holds_no_content(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    await _seed(db_session, bot)
    for path in _expected():
        assert (await _read(client, path)).status_code == 200, path
    logged = " ".join(f"{action} {target}" for action, target, _, _ in await _stored(db_session))
    assert logged
    # 正文、摘要、标题里的机密片段，路径的两段，待跟进的标题：一样都不在。
    for piece in (SECRET, "规则", "口吻", "续约"):
        assert piece not in logged

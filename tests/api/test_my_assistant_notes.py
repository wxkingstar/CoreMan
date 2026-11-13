"""「我的助理」页的知识库：所有者看索引、读写每一页（含置顶页），返回里写明是谁改的；别人碰不到。"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from coreman.core.assistants import keys, notes
from coreman.core.db.models import Bot
from tests.api.my_assistant_helpers import URL, as_bot_token, code, owned
from tests.integration.assistant_helpers import SECRET, raw

pytestmark = pytest.mark.usefixtures("assistant_master")
INDEX, PAGE = f"{URL}/notes", f"{URL}/notes/page"
# 路径本身是个人内容：读与删把它放在请求体里，不进网址（也就不进访问日志）。
READ, DELETE = f"{URL}/notes/read", f"{URL}/notes/delete"
PATH = "规则/口吻"
STORED = (
    "SELECT string_agg(path_enc || title_enc || summary_enc || body_enc, chr(32))"
    " FROM assistant_pages WHERE bot_id = :bot"
)


async def _by_assistant(db_session: AsyncSession, bot: Bot) -> None:
    """助理在所有者发起的一轮里写下并置顶的一页。"""
    await notes.write(
        db_session,
        bot.id,
        PATH,
        body=f"回复要短。{SECRET}",
        summary="口吻规则",
        pinned=True,
        updated_by="assistant",
    )
    await db_session.commit()


async def test_the_owner_creates_reads_and_lists_pages(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await owned(client, db_session)
    created = await client.put(
        PAGE,
        json={"path": " 人//李四 ", "body": f"采购负责人。{SECRET}", "summary": "李四是谁"},
    )
    assert created.status_code == 200, created.text
    entry = created.json()["data"]
    assert entry["path"] == "人/李四" and entry["title"] == "李四" and entry["version"] == 1
    assert entry["pinned"] is False and entry["updated_by"] == "owner"
    assert SECRET not in (await raw(db_session, STORED, bot=bot.id))

    with capture_logs() as logs:
        index = await client.get(INDEX)
        page = await client.post(READ, json={"path": "人/李四"})
    assert index.headers["cache-control"] == "no-store"
    assert index.json()["data"]["locked"] is False
    assert [item["path"] for item in index.json()["data"]["items"]] == ["人/李四"]
    assert "body" not in index.json()["data"]["items"][0]
    assert page.json()["data"]["body"] == f"采购负责人。{SECRET}"
    assert page.json()["data"]["updated_by"] == "owner"
    seen = [
        (entry["action"], entry["target"], entry["actor_id"])
        for entry in logs
        if entry["event"] == "assistant_content_access"
    ]
    assert seen == [("notes.index", "", str(owner.id)), ("notes.read", "page", str(owner.id))]
    # 路径本身是个人内容：不进访问记录。
    assert "李四" not in str(logs) and SECRET not in str(logs)


async def test_the_owner_can_rewrite_and_unpin_a_pinned_page(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    await _by_assistant(db_session, bot)
    [listed] = (await client.get(INDEX)).json()["data"]["items"]
    assert listed["pinned"] is True and listed["updated_by"] == "assistant"

    rewritten = await client.put(
        PAGE,
        json={"path": PATH, "body": "回复要短，先说结论。", "summary": "口吻规则", "version": 1},
    )
    assert rewritten.status_code == 200, rewritten.text
    entry = rewritten.json()["data"]
    assert entry["updated_by"] == "owner" and entry["version"] == 2 and entry["pinned"] is True

    # 同样的改写放在所有者不在场的轮次里是被拒绝的：页面是改置顶页的唯一入口。
    with pytest.raises(notes.NotesError) as refused:
        await notes.write(
            db_session, bot.id, PATH, body="x", summary="", version=2, owner_present=False
        )
    assert refused.value.code == notes.PIN_REQUIRES_OWNER
    await db_session.rollback()

    unpinned = await client.put(
        PAGE,
        json={
            "path": PATH,
            "body": "回复要短，先说结论。",
            "summary": "口吻规则",
            "pinned": False,
            "version": 2,
        },
    )
    assert unpinned.json()["data"]["pinned"] is False
    assert unpinned.json()["data"]["updated_by"] == "owner"
    assert unpinned.json()["data"]["version"] == 3


@pytest.mark.parametrize(
    ("body", "status", "reason"),
    [
        ({"path": PATH, "body": "x", "version": 0}, 409, "page_exists"),
        ({"path": PATH, "body": "x", "version": 9}, 409, "version_conflict"),
        ({"path": "不存在的页", "body": "x", "version": 3}, 404, "page_not_found"),
        ({"path": "a/../b", "body": "x"}, 422, "invalid_path"),
        ({"path": PATH, "body": "   ", "version": 1}, 422, "invalid_page"),
        ({"path": PATH, "body": "x", "summary": "长" * 301, "version": 1}, 422, "invalid_page"),
        ({"path": PATH, "body": "x", "pinned": "yes", "version": 1}, 422, "bool_type"),
        ({"path": PATH, "body": "x", "version": 1, "updated_by": "import"}, 422, "extra_forbidden"),
        ({"path": PATH, "version": 1}, 422, "missing"),
    ],
)
async def test_bad_writes_are_refused_with_a_reason(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    body: dict[str, object],
    status: int,
    reason: str | None,
) -> None:
    bot, _owner = await owned(client, db_session)
    await _by_assistant(db_session, bot)
    response = await client.put(PAGE, json=body)
    assert response.status_code == status, response.text
    assert code(response) == reason
    if reason == "version_conflict":
        assert response.json()["code"] == 40901
        assert response.json()["errors"][0]["current_version"] == 1
    page = await notes.read(db_session, bot.id, PATH)
    assert page is not None and page.version == 1 and page.updated_by == "assistant"


async def test_a_page_over_the_size_limit_is_refused(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await owned(client, db_session)
    response = await client.put(PAGE, json={"path": "大页", "body": "字" * 70_000})
    assert response.status_code == 422 and code(response) == "page_too_large"


async def test_reading_and_deleting(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    bot, _owner = await owned(client, db_session)
    await _by_assistant(db_session, bot)
    missing = await client.post(READ, json={"path": "没有这一页"})
    assert missing.status_code == 404 and code(missing) == "page_not_found"
    assert (await client.post(READ, json={"path": "a/./b"})).status_code == 422
    assert (await client.post(READ, json={})).status_code == 422
    # 置顶页所有者也能删。
    assert (await client.post(DELETE, json={"path": PATH})).json()["data"] == {"deleted": True}
    assert (await client.post(DELETE, json={"path": PATH})).json()["data"] == {"deleted": False}
    assert await notes.read(db_session, bot.id, PATH) is None


async def test_a_page_path_never_travels_in_the_url(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    await _by_assistant(db_session, bot)
    # 带路径参数的读与删都不存在：只有请求体里带路径的那两个接口。
    assert (await client.get(PAGE, params={"path": PATH})).status_code in (404, 405)
    assert (await client.delete(PAGE, params={"path": PATH})).status_code in (404, 405)
    # 索引不收目录参数：给了也不按它筛（目录名同样是路径的一部分）。
    await client.put(PAGE, json={"path": "人/李四", "body": "x"})
    listed = await client.get(INDEX, params={"folder": "规则"})
    assert sorted(item["path"] for item in listed.json()["data"]["items"]) == sorted(
        [PATH, "人/李四"]
    )
    # 读与删的请求体只收 `path` 一个键。
    assert (await client.post(READ, json={"path": PATH, "x": 1})).status_code == 422
    assert (await client.post(DELETE, json={"path": PATH, "x": 1})).status_code == 422
    assert await notes.read(db_session, bot.id, PATH) is not None


async def test_writes_need_csrf_and_an_interactive_login(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, owner = await owned(client, db_session)
    await _by_assistant(db_session, bot)
    csrf = client.headers.pop("X-CSRF-Token")
    assert (
        await client.put(PAGE, json={"path": PATH, "body": "x", "version": 1})
    ).status_code == 403
    assert (await client.post(DELETE, json={"path": PATH})).status_code == 403
    assert (await client.post(READ, json={"path": PATH})).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    as_bot_token(client, monkeypatch, owner)
    for response in (
        await client.get(INDEX),
        await client.post(READ, json={"path": PATH}),
        await client.put(PAGE, json={"path": PATH, "body": "x", "version": 1}),
        await client.post(DELETE, json={"path": PATH}),
    ):
        assert response.status_code == 403 and SECRET not in response.text
    page = await notes.read(db_session, bot.id, PATH)
    assert page is not None and page.version == 1


async def test_an_admin_with_their_own_assistant_cannot_reach_the_owners_pages(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    await _by_assistant(db_session, bot)
    theirs, _admin = await owned(client, db_session, role="platform_admin")
    assert (await client.get(INDEX)).json()["data"]["items"] == []
    read = await client.post(READ, json={"path": PATH})
    assert read.status_code == 404 and SECRET not in read.text
    # 同一个路径在管理员自己的知识库里是另一页：写与删都只落在他自己那边。
    written = await client.put(PAGE, json={"path": PATH, "body": "管理员自己的", "version": 0})
    assert written.status_code == 200
    assert (await client.post(DELETE, json={"path": PATH})).json()["data"] == {"deleted": True}
    page = await notes.read(db_session, bot.id, PATH)
    assert (
        page is not None and page.body == f"回复要短。{SECRET}" and page.updated_by == "assistant"
    )
    assert await notes.read(db_session, theirs.id, PATH) is None


async def test_after_the_key_is_destroyed_the_index_is_locked(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await owned(client, db_session)
    await _by_assistant(db_session, bot)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    index = await client.get(INDEX)
    assert index.status_code == 200 and index.json()["data"] == {"items": [], "locked": True}
    for response in (
        await client.post(READ, json={"path": PATH}),
        await client.put(PAGE, json={"path": PATH, "body": "x", "version": 1}),
        await client.post(DELETE, json={"path": PATH}),
    ):
        assert response.status_code == 409 and code(response) == "content_locked"
        assert SECRET not in response.text

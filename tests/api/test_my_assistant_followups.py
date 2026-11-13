"""「我的助理」页的待跟进：所有者按四类看到自己的条目，可以新增、改、推后、关闭；别人碰不到。"""

from __future__ import annotations

from datetime import date

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from coreman.core.assistants import followups, keys
from coreman.core.db.models import AssistantFollowup, Bot
from tests.api.my_assistant_helpers import URL, as_bot_token, code, owned
from tests.integration.assistant_helpers import SECRET, raw

pytestmark = pytest.mark.usefixtures("assistant_master")
ITEMS = f"{URL}/followups"
STORED = "SELECT string_agg(title_enc, chr(32)) FROM assistant_followups WHERE bot_id = :bot"
COUNT = "SELECT count(*) FROM assistant_followups WHERE bot_id = :bot"


async def _seed(db_session: AsyncSession, bot: Bot) -> None:
    await followups.add(
        db_session, bot.id, category="promised", title=f"给李四报价 {SECRET}", notes="他周一问的"
    )
    await followups.add(
        db_session, bot.id, category="remind", title="续约", due_on=date(2000, 1, 1)
    )
    await db_session.commit()


async def _row(db_session: AsyncSession, bot: Bot, seq: int) -> AssistantFollowup:
    db_session.expire_all()
    # 过期之后先把助理记录读回来：之后取 `bot.id` 不能在同步上下文里触发加载。
    await db_session.refresh(bot)
    return await followups.get(db_session, bot.id, seq)


async def test_the_owner_sees_open_items_in_four_groups(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await owned(client, db_session)
    await _seed(db_session, bot)
    with capture_logs() as logs:
        response = await client.get(ITEMS)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    assert list(data["groups"]) == ["decide", "promised", "waiting", "remind"]
    assert data["total"] == 2 and data["groups"]["decide"] == []
    [promised] = data["groups"]["promised"]
    assert promised["id"] == "F001" and promised["title"] == f"给李四报价 {SECRET}"
    assert promised["notes"] == "他周一问的" and promised["due_today"] is False
    [remind] = data["groups"]["remind"]
    assert remind["id"] == "F002" and remind["due_today"] is True
    assert [
        (entry["action"], entry["target"], entry["actor_id"])
        for entry in logs
        if entry["event"] == "assistant_content_access"
    ] == [("followups.read", "open", str(owner.id))]
    assert SECRET not in str(logs)


async def test_the_owner_adds_edits_snoozes_and_closes(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    added = await client.post(
        ITEMS,
        json={"category": "waiting", "title": f"  等张三回复 {SECRET} ", "due_on": "2099-03-01"},
    )
    assert added.status_code == 200, added.text
    item = added.json()["data"]
    assert item["id"] == "F001" and item["title"] == f"等张三回复 {SECRET}"
    assert item["category"] == "waiting" and item["due_on"] == "2099-03-01"
    assert SECRET not in (await raw(db_session, STORED, bot=bot.id))

    patched = await client.patch(
        f"{ITEMS}/F001", json={"category": "decide", "notes": "改成我来定", "due_on": None}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["category"] == "decide"
    assert patched.json()["data"]["notes"] == "改成我来定"
    assert patched.json()["data"]["due_on"] is None
    assert patched.json()["data"]["title"] == f"等张三回复 {SECRET}"

    snoozed = await client.post(f"{ITEMS}/f1/snooze", json={"until": "2099-01-01"})
    assert snoozed.json()["data"]["status"] == "snoozed"
    assert snoozed.json()["data"]["review_on"] == "2099-01-01"
    # 推后的条目仍是未结的：列表里还在。
    assert (await client.get(ITEMS)).json()["data"]["total"] == 1

    closed = await client.post(f"{ITEMS}/F001/close", json={"dropped": True})
    assert closed.json()["data"]["status"] == "dropped"
    assert (await client.get(ITEMS)).json()["data"]["total"] == 0
    assert (await _row(db_session, bot, 1)).status == "dropped"
    # 已经关了的再关一次原样返回；再改就拒绝。
    assert (await client.post(f"{ITEMS}/F001/close", json={})).json()["data"]["status"] == "dropped"
    again = await client.patch(f"{ITEMS}/F001", json={"title": "x"})
    assert again.status_code == 409 and code(again) == "followup_closed"


@pytest.mark.parametrize(
    ("method", "path", "body", "status", "reason"),
    [
        ("post", "", {"category": "someday", "title": "x"}, 422, "invalid_category"),
        ("post", "", {"category": "remind", "title": "   "}, 422, "invalid_title"),
        ("post", "", {"category": "remind", "title": "x", "extra": 1}, 422, "extra_forbidden"),
        ("patch", "/F001", {}, 422, "nothing_to_change"),
        ("patch", "/F001", {"title": None}, 422, "null_not_allowed"),
        ("patch", "/F001", {"category": None}, 422, "null_not_allowed"),
        ("patch", "/F099", {"title": "x"}, 404, "followup_not_found"),
        ("patch", "/nonsense", {"title": "x"}, 404, "followup_not_found"),
        ("post", "/F001/snooze", {"until": "2000-01-01"}, 422, "snooze_not_future"),
        ("post", "/F001/snooze", {"until": "明天"}, 422, "date_from_datetime_parsing"),
        ("post", "/F001/close", {"dropped": "yes"}, 422, "bool_type"),
    ],
)
async def test_bad_requests_are_refused_with_a_reason(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    method: str,
    path: str,
    body: dict[str, object],
    status: int,
    reason: str | None,
) -> None:
    bot, _owner = await owned(client, db_session)
    await _seed(db_session, bot)
    response = await client.request(method, f"{ITEMS}{path}", json=body)
    assert response.status_code == status, response.text
    assert code(response) == reason
    row = await _row(db_session, bot, 1)
    assert row.status == "open" and row.title_enc == f"给李四报价 {SECRET}"
    assert await raw(db_session, COUNT, bot=bot.id) == 2


async def test_writes_need_csrf_and_an_interactive_login(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, owner = await owned(client, db_session)
    await _seed(db_session, bot)
    csrf = client.headers.pop("X-CSRF-Token")
    for method, path, body in (
        ("post", "", {"category": "remind", "title": "x"}),
        ("patch", "/F001", {"title": "x"}),
        ("post", "/F001/close", {}),
        ("post", "/F001/snooze", {"until": "2099-01-01"}),
    ):
        assert (await client.request(method, f"{ITEMS}{path}", json=body)).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    as_bot_token(client, monkeypatch, owner)
    assert (await client.get(ITEMS)).status_code == 403
    assert (await client.post(f"{ITEMS}/F001/close", json={})).status_code == 403
    assert (await _row(db_session, bot, 1)).status == "open"
    assert await raw(db_session, COUNT, bot=bot.id) == 2


async def test_an_admin_with_their_own_assistant_cannot_reach_the_owners_items(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    await _seed(db_session, bot)
    await owned(client, db_session, role="platform_admin")
    listed = await client.get(ITEMS)
    assert listed.json()["data"]["total"] == 0 and SECRET not in listed.text
    for response in (
        await client.patch(f"{ITEMS}/F001", json={"title": "改掉"}),
        await client.post(f"{ITEMS}/F001/close", json={}),
        await client.post(f"{ITEMS}/F001/snooze", json={"until": "2099-01-01"}),
    ):
        assert response.status_code == 404 and SECRET not in response.text
    row = await _row(db_session, bot, 1)
    assert row.status == "open" and row.title_enc == f"给李四报价 {SECRET}"


async def test_after_the_key_is_destroyed_items_show_as_locked_and_cannot_be_written(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await owned(client, db_session)
    await _seed(db_session, bot)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    response = await client.get(ITEMS)
    assert response.status_code == 200 and SECRET not in response.text
    [promised] = response.json()["data"]["groups"]["promised"]
    assert promised["locked"] is True and promised["title"] == "" and promised["notes"] is None
    refused = await client.post(ITEMS, json={"category": "remind", "title": "x"})
    assert refused.status_code == 409 and code(refused) == "content_locked"
    # 只动元数据的操作不需要密钥。
    assert (await client.post(f"{ITEMS}/F001/close", json={})).status_code == 200


async def test_reading_still_works_while_stopping_but_writing_does_not(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    await _seed(db_session, bot)
    bot.assistant_state = "deprovisioning"
    await db_session.commit()
    assert (await client.get(ITEMS)).json()["data"]["total"] == 2
    refused = await client.post(f"{ITEMS}/F001/close", json={})
    assert refused.status_code == 409 and code(refused) == "assistant_not_active"

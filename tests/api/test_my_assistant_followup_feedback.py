"""「我的助理」页上对待跟进的三个动作：补充（立刻交回助理）、带备注改期、按原因关闭；每个都记一条
所有者反馈，原话只以密文落库。"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import followups, sealed
from coreman.core.db.models import AssistantFeedback, AssistantInbox, Bot
from tests.api.my_assistant_helpers import URL, code, owned
from tests.integration.assistant_helpers import SECRET, raw

pytestmark = pytest.mark.usefixtures("assistant_master")
ITEMS = f"{URL}/followups"
NOTES = (
    "SELECT string_agg(coalesce(note_enc, ''), chr(32)) FROM assistant_feedback WHERE bot_id = :bot"
)


async def _seed(db_session: AsyncSession, bot: Bot) -> None:
    row = await followups.add(db_session, bot.id, category="promised", title="对账")
    await followups.update(db_session, bot.id, row.seq, stuck="how")
    await followups.add(db_session, bot.id, category="waiting", title="等报价")
    await db_session.commit()


async def _feedback(db_session: AsyncSession, bot_id: uuid.UUID) -> list[AssistantFeedback]:
    db_session.expire_all()
    rows = list(
        await db_session.scalars(
            select(AssistantFeedback)
            .where(AssistantFeedback.bot_id == bot_id)
            .order_by(AssistantFeedback.created_at)
        )
    )
    await sealed.open_rows(db_session, rows)
    return rows


async def test_supplement_writes_the_notes_and_hands_it_back_right_away(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _ = await owned(client, db_session)
    bot_id = bot.id
    await _seed(db_session, bot)
    response = await client.post(f"{ITEMS}/F001/feedback", json={"text": f"  找财务要 {SECRET} "})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["stuck"] is None
    assert data["notes"].endswith(f"所有者补充：找财务要 {SECRET}")
    [entry] = await _feedback(db_session, bot_id)
    assert (entry.kind, entry.seq, entry.code, entry.note_enc) == (
        "supplement",
        1,
        None,
        f"找财务要 {SECRET}",
    )
    assert SECRET not in str(await raw(db_session, NOTES, bot=bot_id))
    inbox = list(
        await db_session.scalars(select(AssistantInbox).where(AssistantInbox.bot_id == bot_id))
    )
    await sealed.open_rows(db_session, inbox)
    [handed] = inbox
    assert handed.level == "immediate" and handed.kind == "reminder"
    assert handed.summary_enc["reminder"] == "owner_feedback"
    assert (
        handed.summary_enc["label"] == "F001" and handed.summary_enc["text"] == f"找财务要 {SECRET}"
    )


async def test_reschedule_with_a_note_and_close_with_a_reason(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _ = await owned(client, db_session)
    bot_id = bot.id
    await _seed(db_session, bot)
    moved = await client.post(
        f"{ITEMS}/F001/snooze", json={"until": "2099-01-05", "note": "等月底结账"}
    )
    assert moved.status_code == 200, moved.text
    item = moved.json()["data"]
    assert item["status"] == "snoozed" and item["review_on"] == "2099-01-05"
    assert item["stuck"] is None and "所有者改期到 2099-01-05：等月底结账" in item["notes"]
    closed = await client.post(f"{ITEMS}/F002/close", json={"reason": "not_needed"})
    assert closed.json()["data"]["status"] == "dropped"
    assert closed.json()["data"]["close_code"] == "not_needed"
    talked = await client.post(f"{ITEMS}/F001/close", json={"reason": "talked", "note": "电话聊过"})
    assert talked.json()["data"]["status"] == "done"
    assert talked.json()["data"]["close_code"] == "talked"
    # 已经关了的再关一次原样返回，不再记反馈。
    again = await client.post(f"{ITEMS}/F001/close", json={"reason": "resolved"})
    assert again.json()["data"]["close_code"] == "talked"
    assert [(e.kind, e.seq, e.code, e.note_enc) for e in await _feedback(db_session, bot_id)] == [
        ("reschedule", 1, "2099-01-05", "等月底结账"),
        ("close", 2, "not_needed", None),
        ("close", 1, "talked", "电话聊过"),
    ]


@pytest.mark.parametrize(
    ("path", "body", "status", "reason"),
    [
        ("/F001/feedback", {"text": "   "}, 422, "invalid_feedback"),
        ("/F001/feedback", {"text": "长" * 1001}, 422, "invalid_feedback"),
        ("/F009/feedback", {"text": "x"}, 404, "followup_not_found"),
        ("/F001/close", {"reason": "bored"}, 422, "invalid_close_code"),
        ("/F001/snooze", {"until": "2000-01-01", "note": "x"}, 422, "snooze_not_future"),
    ],
)
async def test_bad_feedback_is_refused(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    path: str,
    body: dict[str, object],
    status: int,
    reason: str,
) -> None:
    bot, _ = await owned(client, db_session)
    bot_id = bot.id
    await _seed(db_session, bot)
    response = await client.post(f"{ITEMS}{path}", json=body)
    assert response.status_code == status, response.text
    assert code(response) == reason
    assert await _feedback(db_session, bot_id) == []
    assert (await followups.get(db_session, bot_id, 1)).status == "open"


async def test_closed_items_take_no_supplement(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _ = await owned(client, db_session)
    await _seed(db_session, bot)
    await client.post(f"{ITEMS}/F002/close", json={"reason": "resolved"})
    response = await client.post(f"{ITEMS}/F002/feedback", json={"text": "x"})
    assert response.status_code == 409 and code(response) == "followup_closed"

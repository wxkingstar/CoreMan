"""私聊里「托管设置」卡片：只改卡片上列出的会话、只认所有者与平台签的令牌、过期作废。"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import chat_choices, settings
from coreman.core.timeutils import utcnow
from coreman.runtime.worker import assistant_scope
from tests.integration.lifecycle_helpers import CIPHER
from tests.integration.watch_helpers import Watched, assistant

pytestmark = pytest.mark.usefixtures("assistant_master")
CHOICES = [
    chat_choices.Choice("oc_team", "项目群", "group"),
    chat_choices.Choice("oc_li", "李四", "p2p"),
]


def _inbound(who: Watched, sender: str | None = None) -> Any:
    return SimpleNamespace(
        id=1,
        chat_id=who.chat_id,
        reply_context={"message_id": "om_card"},
        sender_platform_user_id=sender or who.pid,
    )


async def _submit(
    session: AsyncSession, who: Watched, card: dict[str, Any], picked: list[str], **kw: Any
) -> str:
    form = card["body"]["elements"][1]["elements"]
    value = dict(form[-1]["behaviors"][0]["value"])
    value.update(kw.pop("override", {}))
    action = {**value, "selected": {"chats": picked}}
    ctx = SimpleNamespace(cipher=CIPHER)
    return await assistant_scope.handle(session, ctx, who.bot, _inbound(who, **kw), action)  # type: ignore[arg-type]


async def test_the_card_changes_only_the_chats_it_listed(db_session: AsyncSession) -> None:
    who = await assistant(db_session)
    await settings.update(db_session, who.bot.id, now=utcnow(), ignored_chats=["oc_old", "oc_li"])
    row = await settings.get(db_session, who.bot.id)
    card = assistant_scope.card(CIPHER, who.bot.id, 7, time.time(), row, CHOICES)
    select = card["body"]["elements"][1]["elements"][0]
    assert select["selected_values"] == ["oc_li"]
    assert [option["text"]["content"] for option in select["options"]] == [
        "群：项目群",
        "单聊：李四",
    ]
    # 取消李四、勾上项目群；不在卡片上的 oc_old 原样留着。
    assert await _submit(db_session, who, card, ["oc_team"]) == "scope_saved"
    shown = settings.view(await settings.get(db_session, who.bot.id), utcnow())
    assert shown["ignored_chats"] == ["oc_old", "oc_team"]


async def test_only_the_owner_with_a_fresh_signed_card_counts(db_session: AsyncSession) -> None:
    who = await assistant(db_session)
    row = await settings.get(db_session, who.bot.id)
    card = assistant_scope.card(CIPHER, who.bot.id, 7, time.time(), row, CHOICES)
    assert await _submit(db_session, who, card, ["oc_team"], sender="pid_someone") == "ignored"
    # 篡改卡片上「预先勾上的会话」：令牌对不上。
    tampered = {"shown": ["oc_boss"]}
    assert await _submit(db_session, who, card, [], override=tampered) == "ignored"
    old = assistant_scope.card(CIPHER, who.bot.id, 7, time.time() - 3600, row, CHOICES)
    assert await _submit(db_session, who, old, ["oc_team"]) == "scope_expired"
    assert (
        settings.view(await settings.get(db_session, who.bot.id), utcnow())["ignored_chats"] == []
    )


async def test_in_listed_scope_the_card_picks_the_managed_chats(db_session: AsyncSession) -> None:
    who = await assistant(db_session)
    await settings.update(
        db_session, who.bot.id, now=utcnow(), chat_scope="listed", managed_chats=["oc_team"]
    )
    row = await settings.get(db_session, who.bot.id)
    card = assistant_scope.card(CIPHER, who.bot.id, 7, time.time(), row, CHOICES)
    assert await _submit(db_session, who, card, ["oc_team", "oc_li"]) == "scope_saved"
    shown = settings.view(await settings.get(db_session, who.bot.id), utcnow())
    assert shown["managed_chats"] == ["oc_team", "oc_li"] and shown["ignored_chats"] == []
    # 发卡片之后换回了托管全部：这张卡片勾的是另一份名单，作废。
    await settings.update(db_session, who.bot.id, now=utcnow(), chat_scope="all")
    assert await _submit(db_session, who, card, []) == "scope_stale"

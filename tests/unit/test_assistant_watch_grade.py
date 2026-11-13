"""分级表（spec 第五节）的每一行，以及压着与攒批的时间窗。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from coreman.core.assistants.watch_grade import (
    BATCH_WINDOW,
    HOLD_WINDOW,
    Context,
    Item,
    Level,
    grade,
    release_at,
)
from coreman.core.db.models import AssistantSettings

OWNER = "ou_owner"
NOW = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)


def prefs() -> AssistantSettings:
    return AssistantSettings(
        bot_id=uuid.uuid4(),
        open_id=OWNER,
        focus_chats_enc=["oc_focus"],
        ignored_chats_enc=["oc_noise"],
    )


def item(**over: Any) -> Item:
    values: dict[str, Any] = {
        "chat_id": "oc_team",
        "chat_kind": "group",
        "message_id": "om_1",
        "sender_id": "ou_li",
        "sender_type": "user",
        "at_ms": int(NOW.timestamp() * 1000),
    }
    values.update(over)
    return Item(**values)


P2P = {"chat_id": "oc_li", "chat_kind": "p2p"}
BOT = {"sender_type": "app", "sender_id": "cli_other"}
AT_ME = {"mentions": frozenset({OWNER})}
NO_CHAT = {"chat_id": "", "chat_kind": ""}
# （说明, 条目, 所有者多久前在这个会话里发过言, 期望的级别）
TABLE: list[tuple[str, Item, timedelta | None, Level]] = [
    ("私聊", item(**P2P), None, Level.IMMEDIATE),
    ("群里 @ 本人", item(**AT_ME), None, Level.IMMEDIATE),
    ("群里机器人 @ 本人", item(**BOT, **AT_ME), None, Level.IMMEDIATE),
    ("审批被拒或执行失败", item(kind="approval_outcome", **NO_CHAT), None, Level.IMMEDIATE),
    ("重点群里的消息", item(chat_id="oc_focus"), None, Level.IMMEDIATE),
    ("其他群里真人的消息", item(), None, Level.BATCH),
    ("群里 @ 了别人", item(mentions=frozenset({"ou_wang"})), None, Level.BATCH),
    ("新出现的飞书审批待办", item(kind="approval_todo", **NO_CHAT), None, Level.BATCH),
    ("私聊里机器人发来的通知", item(**P2P, **BOT), None, Level.BATCH),
    ("所有者 10 分钟内在该会话发过言", item(**P2P), timedelta(minutes=9), Level.HOLD),
    ("所有者刚在重点群里发过言", item(chat_id="oc_focus"), timedelta(minutes=1), Level.HOLD),
    ("所有者刚发过言的群里 @ 本人", item(**AT_ME), timedelta(seconds=5), Level.HOLD),
    ("他停下已满 10 分钟", item(**P2P), timedelta(minutes=10), Level.IMMEDIATE),
    ("所有者自己发的", item(sender_id=OWNER), None, Level.IGNORE),
    ("所有者自己在私聊里发的", item(**P2P, sender_id=OWNER), None, Level.IGNORE),
    ("群里没 @ 他的机器人消息", item(**BOT), None, Level.IGNORE),
    ("重点群里没 @ 他的机器人消息", item(chat_id="oc_focus", **BOT), None, Level.IGNORE),
    ("所有者设为忽略的会话", item(chat_id="oc_noise", **AT_ME), None, Level.IGNORE),
    ("忽略的会话里他刚发过言", item(chat_id="oc_noise"), timedelta(minutes=1), Level.IGNORE),
    ("系统消息", item(**P2P, msg_type="system", sender_type=""), None, Level.IGNORE),
    ("已撤回的消息", item(**P2P, deleted=True), None, Level.IGNORE),
]


@pytest.mark.parametrize(("why", "one", "ago", "level"), TABLE, ids=[row[0] for row in TABLE])
def test_every_row_of_the_table(why: str, one: Item, ago: timedelta | None, level: Level) -> None:
    spoke = NOW - ago if ago is not None else None
    assert grade(one, prefs(), Context(now=NOW, owner_spoke_at=spoke)) is level, why


def test_settings_without_lists_grade_like_empty_lists() -> None:
    bare = AssistantSettings(bot_id=uuid.uuid4(), open_id=OWNER)
    assert grade(item(chat_id="oc_focus"), bare, Context(now=NOW)) is Level.BATCH
    assert grade(item(chat_id="oc_noise"), bare, Context(now=NOW)) is Level.BATCH


def test_levels_are_the_stored_values() -> None:
    assert [level.value for level in Level] == ["immediate", "batch", "hold", "ignore"]
    assert item().created_at == NOW
    assert (HOLD_WINDOW, BATCH_WINDOW) == (timedelta(minutes=10), timedelta(minutes=5))


def test_release_times() -> None:
    quiet = Context(now=NOW)
    spoke = NOW - timedelta(minutes=4)
    assert release_at(Level.IMMEDIATE, quiet, batch_until=None) is None
    # 攒批：没有在攒的一批就从现在起 5 分钟；有就并进去，一起到点。
    assert release_at(Level.BATCH, quiet, batch_until=None) == NOW + timedelta(minutes=5)
    soon = NOW + timedelta(minutes=2)
    assert release_at(Level.BATCH, quiet, batch_until=soon) == soon
    stale = NOW - timedelta(seconds=1)
    assert release_at(Level.BATCH, quiet, batch_until=stale) == NOW + timedelta(minutes=5)
    # 压着：到他这次发言之后 10 分钟。
    held = Context(now=NOW, owner_spoke_at=spoke)
    assert release_at(Level.HOLD, held, batch_until=soon) == spoke + timedelta(minutes=10)

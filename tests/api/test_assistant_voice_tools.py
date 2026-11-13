"""个人工具 MCP 上，口吻样本那一轮只有一个工具：别人的内容没有任何读取途径。"""

from __future__ import annotations

from typing import Any

import httpx

from coreman.core import personal_schedules as schedules
from coreman.core.assistants import notes
from coreman.core.assistants.kinds import COLD_START_KIND, HANDOVER_KIND
from tests.api.test_assistant_approval_gate import listed, owner_turn
from tests.api.test_assistant_tool_groups import call, platform_turn
from tests.api.test_feishu_personal import headers

INVALID = "invalid_tool_or_arguments"
SESSION = "oc_private#job:cold-start"


async def test_a_cold_start_turn_lists_one_tool_and_runs_nothing_else(
    client: httpx.AsyncClient, app: Any, db_session: Any
) -> None:
    bot, user, turn = await platform_turn(db_session, app, COLD_START_KIND, session_key=SESSION)
    assert set(await listed(client, app, turn, user)) == {"assistant_voice_save"}
    auth = await headers(app, turn, user)
    refused = [
        ("feishu_chat_history", {"chat_id": "oc_li"}),
        ("feishu_authorization_status", {}),
        ("notes_read", {"path": "people/li.md"}),
        ("notes_write", {"path": "profile/voice.md", "body": "x", "summary": "x", "pinned": True}),
        ("followup_list", {"scope": "open"}),
        ("approval_list", {}),
        ("assistant_settings_get", {}),
        (next(iter(schedules.TOOLS)), {}),
    ]
    for name, arguments in refused:
        out = await call(client, auth, name, arguments)
        assert out["error"] == INVALID, name
    saved = await call(
        client, auth, "assistant_voice_save", {"body": "说话简短，常用「收到」开头。"}
    )
    assert saved == {"status": "saved", "path": "profile/voice.md", "version": 1, "pinned": True}
    page = await notes.read(db_session, bot.id, "profile/voice.md")
    assert page is not None and page.pinned


async def test_other_turns_never_see_the_voice_tool(
    client: httpx.AsyncClient, app: Any, db_session: Any
) -> None:
    _, user, turn = await platform_turn(db_session, app, HANDOVER_KIND)
    assert "assistant_voice_save" not in await listed(client, app, turn, user)
    auth = await headers(app, turn, user)
    out = await call(client, auth, "assistant_voice_save", {"body": "x"})
    assert out["error"] == INVALID


async def test_the_owners_own_turn_does_not_see_it_either(
    client: httpx.AsyncClient, app: Any, db_session: Any
) -> None:
    _, user, task = await owner_turn(db_session, app, assistant=True)
    listing = await listed(client, app, task, user)
    assert "assistant_voice_save" not in listing and "feishu_chat_history" in listing
    auth = await headers(app, task, user)
    out = await call(client, auth, "assistant_voice_save", {"body": "x"})
    assert out["error"] == INVALID

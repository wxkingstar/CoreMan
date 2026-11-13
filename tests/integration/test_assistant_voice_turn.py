"""口吻样本那一轮：独立会话、文本只有所有者的样本；专用入口写出置顶页；工具只认这类任务。"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import kinds, notes, settings, voice, voice_tools
from coreman.core.assistants import tools as registry
from coreman.core.db.models import Task
from coreman.runtime.worker.assistant_cold_start import AssistantColdStartHandler
from tests.integration.approval_helpers import assistant_turn
from tests.integration.assistant_helpers import SECRET, raw
from tests.integration.test_assistant_platform_turn_handler import assistant, run
from tests.integration.test_cron_handler import claim
from tests.integration.worker_helpers import turn_block, user_input

pytestmark = pytest.mark.usefixtures("assistant_master")
SAMPLES = ["收到，我看一下", "可以，周四下班前给你", "这个先放一放", "辛苦了", "明天上午对一下"]
AUTHOR = "SELECT updated_by FROM assistant_pages WHERE bot_id = :bot"


def cold_start_scope(scope: Any) -> Any:
    """这一轮核验后的身份，换成冷启动那类任务的（工具分派只看任务类型）。"""
    task = cast(Task, SimpleNamespace(kind=kinds.COLD_START_KIND))
    return replace(scope, task=task, event=None, platform=True)


async def test_save_writes_one_pinned_page_as_the_assistant(db_session: AsyncSession) -> None:
    bot, _, _ = await assistant_turn(db_session)
    entry = await voice.save(db_session, bot.id, "说话简短，常用「收到」开头。")
    await db_session.commit()
    assert (entry.path, entry.pinned, entry.version) == (voice.PATH, True, 1)
    page = await notes.read(db_session, bot.id, "profile/voice.md")
    assert page is not None and page.pinned and page.title == voice.TITLE
    assert await raw(db_session, AUTHOR, bot=bot.id) == "assistant"
    # 再写一次是整页替换。
    again = await voice.save(db_session, bot.id, "说话简短。")
    assert again.version == 2 and again.pinned
    with pytest.raises(notes.NotesError) as caught:
        await voice.save(db_session, bot.id, "长" * voice.MAX_BODY_BYTES)
    assert caught.value.code == "page_too_large"


@pytest.mark.parametrize("author", ["owner", "import"])
async def test_a_voice_page_the_owner_wrote_is_never_overwritten(
    db_session: AsyncSession, author: str
) -> None:
    """所有者自己改过（或导入的）口吻页是他的话；再跑一次冷启动不能拿归纳出来的盖掉它。"""
    bot, _, scope = await assistant_turn(db_session)
    mine = "我说话就这样：短句，不用敬语。"
    await notes.write(
        db_session,
        bot.id,
        voice.PATH,
        body=mine,
        summary="我自己写的",
        pinned=True,
        updated_by=author,
    )
    await db_session.commit()
    assert await voice.save(db_session, bot.id, "说话简短，常用「收到」开头。") is None
    out = await voice_tools.dispatch(
        db_session,
        None,  # type: ignore[arg-type]
        cold_start_scope(scope),
        "assistant_voice_save",
        {"body": "归纳出来的另一版"},
    )
    await db_session.commit()
    assert out["status"] == "kept" and out["path"] == voice.PATH and "error" not in out
    page = await notes.read(db_session, bot.id, voice.PATH)
    assert page is not None and (page.body, page.version, page.summary) == (mine, 1, "我自己写的")
    assert await raw(db_session, AUTHOR, bot=bot.id) == author


async def test_the_tool_only_works_in_a_cold_start_turn(db_session: AsyncSession) -> None:
    bot, _, owner = await assistant_turn(db_session)
    body = {"body": "说话简短，常用「收到」开头。"}
    # 所有者自己的一轮、别的平台来源轮次都用不了它。
    for scope in (owner, replace(owner, event=None, platform=True)):
        out = await voice_tools.dispatch(db_session, None, scope, "assistant_voice_save", body)  # type: ignore[arg-type]
        assert out == {"error": "invalid_tool_or_arguments"}
    assert await notes.read(db_session, bot.id, voice.PATH) is None
    turn = cold_start_scope(owner)
    out = await voice_tools.dispatch(db_session, None, turn, "assistant_voice_save", body)  # type: ignore[arg-type]
    await db_session.commit()
    assert out == {"status": "saved", "path": voice.PATH, "version": 1, "pinned": True}
    # 入参里没有路径、置顶这类可选项：写哪一页不由模型决定。
    schema = voice_tools.DEFINITIONS[0]["inputSchema"]
    assert set(schema["properties"]) == {"body"}
    extra = await voice_tools.dispatch(
        db_session,
        None,
        turn,
        "assistant_voice_save",
        {**body, "path": "rules/a.md"},  # type: ignore[arg-type]
    )
    assert extra["error"] == "invalid_tool_or_arguments"


def test_a_cold_start_turn_gets_the_voice_group_and_nothing_else() -> None:
    assert registry.groups_for(kinds.COLD_START_KIND) == ("voice",)
    assert registry.bare(("voice",)) and not registry.bare(registry.GROUPS)
    assert "voice" not in registry.GROUPS
    assert registry.group_of("assistant_voice_save") == "voice"
    base = [
        {"name": "feishu_chat_history", "description": "", "inputSchema": {}},
        {"name": "feishu_authorization_status", "description": "", "inputSchema": {}},
    ]
    listed = {item["name"] for item in registry.definitions(base, groups=("voice",))}
    assert listed == {"assistant_voice_save"}
    # 别的轮次照旧，且没有这个工具。
    usual = {item["name"] for item in registry.definitions(base, groups=registry.GROUPS)}
    assert {"feishu_chat_history", "notes_read", "approval_list"} <= usual
    assert "assistant_voice_save" not in usual


async def test_the_turn_runs_in_its_own_session_with_only_the_samples(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await assistant(db_session)
    # 知识库里已有一页内容来自别人消息的普通页：它的标题与摘要不该进这一轮。
    await notes.write(
        db_session, bot.id, "people/li.md", body="李四的事", summary=f"李四说 {SECRET}"
    )
    row = await settings.get(db_session, bot.id)
    queued = await voice.enqueue(db_session, bot, row, SAMPLES, 1763017200000)
    await db_session.commit()
    assert queued is not None and queued.session_key == "oc_private#job:cold-start"
    assert queued.dedupe_key == f"assistant-cold-start:{bot.id}:1763017200000:voice"
    # 同一次冷启动只排一轮。
    assert await voice.enqueue(db_session, bot, row, SAMPLES, 1763017200000) is None
    task = await claim(db_session)
    done, relay = await run(db_engine, db_session, task, AssistantColdStartHandler())
    assert done.status == "succeeded"
    request = relay.requests[0]
    text = user_input(request)
    assert "冷启动：口吻样本" in text and "> 可以，周四下班前给你" in text
    # 独立会话：没有知识库的首轮注入，别人的内容到不了这一轮。
    assert "个人知识库" not in turn_block(request)
    assert SECRET not in json.dumps(request, ensure_ascii=False)


async def test_too_few_samples_cancel_the_turn(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await assistant(db_session)
    row = await settings.get(db_session, bot.id)
    await voice.enqueue(db_session, bot, row, SAMPLES[:2], 1)
    await db_session.commit()
    task = await claim(db_session)
    done, relay = await run(db_engine, db_session, task, AssistantColdStartHandler())
    assert done.status == "cancelled" and done.error_code == "platform_turn_inactive"
    assert relay.requests == []

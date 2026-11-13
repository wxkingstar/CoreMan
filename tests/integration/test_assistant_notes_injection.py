"""知识库注入：主会话首轮给置顶页与索引，之后只在置顶页有更新时补一条说明。"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, notes, sealed, transcripts
from coreman.core.chat import sessions
from coreman.core.crypto import DecryptError
from coreman.core.db.models import AssistantTranscript, Bot, Task
from coreman.runtime.worker.chat import assistant_notes
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.assistant_helpers import SECRET, into_assistant
from tests.integration.credential_helpers import owner
from tests.integration.test_chat_handler import chat_task
from tests.integration.worker_helpers import build_ctx, turn_block

pytestmark = pytest.mark.usefixtures("assistant_master")
MARK = "个人知识库"
TONE = f"说话简短，先给结论。{SECRET}"


async def assistant_chat(session: AsyncSession) -> tuple[Bot, Task]:
    """所有者在私聊里发来第一条消息的助理，知识库里有一页置顶、一页不置顶（都是一小时前写的）。"""
    bot, user, task, _ = await owner(session)
    await into_assistant(session, bot, user)
    await keys.ensure_key(session, sealed.master(), user.id)
    await notes.write(session, bot.id, "rules/tone.md", body=TONE, summary="口吻", pinned=True)
    await notes.write(
        session, bot.id, "people/li.md", title="李四", summary="负责报价", body="李四的手机号。"
    )
    await session.execute(text("UPDATE assistant_pages SET updated_at = now() - interval '1 hour'"))
    await session.commit()
    return bot, task


async def run(engine: AsyncEngine, session: AsyncSession, task: Task) -> dict:
    relay = FakeRelay("normal")
    ctx = build_ctx(engine, task, relay_client_factory=lambda _r: relay.client())
    await ChatTaskHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    done = await session.get(Task, task.id, populate_existing=True)
    assert done is not None and done.status == "succeeded"
    return relay.requests[0]


async def hand_back(session: AsyncSession, bot: Bot) -> None:
    """节点交回上一轮的转录：下一轮才续得上同一个会话（否则平台会换新会话）。"""
    row = await session.scalar(
        select(AssistantTranscript)
        .where(
            AssistantTranscript.bot_id == bot.id,
            AssistantTranscript.pending_transfer.is_not(None),
        )
        .execution_options(populate_existing=True)
    )
    assert row is not None and row.pending_node_id is not None
    stored = await transcripts.upload(
        session,
        node_id=row.pending_node_id,
        transfer=row.pending_transfer,
        status="ok",
        data=b"\x1f\x8b-transcript",
    )
    await session.commit()
    assert stored == "stored"


async def next_turn(session: AsyncSession, bot: Bot, words: str) -> Task:
    return await chat_task(
        session, bot, words, sender="owner_pid", chat_type="single", chat_id="oc_private"
    )


async def test_first_turn_gets_pinned_pages_then_only_changes(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, task = await assistant_chat(db_session)
    first = await run(db_engine, db_session, task)
    block = turn_block(first)
    assert MARK in block and TONE in block
    assert "- people/li.md — 李四 — 负责报价" in block and "李四的手机号" not in block
    # 个人内容只在经标准输入传的本轮块里，不在 system prompt 里。
    assert SECRET not in first["messages"][0]["content"]

    # 第二轮续上同一个会话，置顶页没动：什么都不带。
    await hand_back(db_session, bot)
    second = await run(db_engine, db_session, await next_turn(db_session, bot, "再看一下"))
    assert second["session_id"] == first["session_id"]
    assert MARK not in turn_block(second)

    # 置顶页改了：第三轮补一条变更说明，只带改过的那一页。
    await notes.write(
        db_session,
        bot.id,
        "rules/tone.md",
        body="说话简短，先给结论，再给理由。",
        summary="口吻",
        version=1,
    )
    await db_session.commit()
    await hand_back(db_session, bot)
    third = await run(db_engine, db_session, await next_turn(db_session, bot, "继续"))
    assert third["session_id"] == first["session_id"]
    block = turn_block(third)
    assert "置顶页有更新" in block and "说话简短，先给结论，再给理由。" in block
    assert "【置顶页（版本 2）】\n> 路径：rules/tone.md" in block and "索引（共" not in block

    # 第四轮：变更已经给过，不再重复。
    await hand_back(db_session, bot)
    fourth = await run(db_engine, db_session, await next_turn(db_session, bot, "好"))
    assert MARK not in turn_block(fourth)


async def test_a_session_that_starts_over_is_a_first_turn_again(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """上一轮的转录没交回来：平台换了新会话，新会话的第一轮重新带上置顶页与索引。"""
    bot, task = await assistant_chat(db_session)
    first = await run(db_engine, db_session, task)
    second = await run(db_engine, db_session, await next_turn(db_session, bot, "再看一下"))
    assert second["session_id"] != first["session_id"]
    block = turn_block(second)
    assert MARK in block and TONE in block and "索引（共 2 页" in block


async def test_over_the_limit_the_first_turn_gets_only_the_index(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, task = await assistant_chat(db_session)
    monkeypatch.setattr(notes, "PINNED_LIMIT_BYTES", 10)
    block = turn_block(await run(db_engine, db_session, task))
    assert MARK in block and "这里不附正文" in block
    assert TONE not in block and "* rules/tone.md — tone — 口吻" in block


async def test_an_empty_knowledge_base_adds_nothing(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, task = await assistant_chat(db_session)
    await db_session.execute(text("DELETE FROM assistant_pages"))
    await db_session.commit()
    assert MARK not in turn_block(await run(db_engine, db_session, task))


async def test_unreadable_pages_do_not_stop_the_turn(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_a: object, **_k: object) -> str:
        raise DecryptError("x")

    bot, task = await assistant_chat(db_session)
    monkeypatch.setattr(notes, "pinned_block", broken)
    assert MARK not in turn_block(await run(db_engine, db_session, task))


async def test_standard_employees_are_untouched(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    _, _, task, _ = await owner(db_session)
    assert MARK not in turn_block(await run(db_engine, db_session, task))


async def test_only_the_main_session_is_injected(db_session: AsyncSession) -> None:
    """子任务会话（键不是私聊会话本身）不注入：它自己用工具读。"""
    bot, task = await assistant_chat(db_session)
    info = sessions.SessionInfo(uuid.uuid4(), True, False)
    ctx = SimpleNamespace(task=task)
    side = SimpleNamespace(bot=bot, session_key="oc_private#job:1", chat_id="oc_private")
    assert await assistant_notes.turn_note(db_session, ctx, side, info) is None  # type: ignore[arg-type]
    main = SimpleNamespace(bot=bot, session_key="oc_private", chat_id="oc_private")
    note = await assistant_notes.turn_note(db_session, ctx, main, info)  # type: ignore[arg-type]
    assert note is not None and note.startswith("- 个人知识库")
    # 换运行时后的首轮：会话不是新的，但节点上没有上文，同样按首轮给。
    carried = sessions.SessionInfo(uuid.uuid4(), False, False, inherited_from=uuid.uuid4())
    again = await assistant_notes.turn_note(db_session, ctx, main, carried)  # type: ignore[arg-type]
    assert again == note
    # 续着的会话却查不到上一轮的记录：按首轮给，宁可多给。
    lost = sessions.SessionInfo(uuid.uuid4(), False, False)
    assert await assistant_notes.turn_note(db_session, ctx, main, lost) == note  # type: ignore[arg-type]

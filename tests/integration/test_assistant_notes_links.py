"""知识库对接 Obsidian 式笔记：带不带 `.md` 是同一页，双链按页名找页，改一处不必整页重写。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import archive, notes, notes_tools, transfer
from coreman.core.db.models import Bot
from tests.integration.approval_helpers import assistant_turn
from tests.integration.assistant_helpers import raw
from tests.integration.test_assistant_notes import assistant, refused
from tests.integration.test_assistant_notes_tools import call

pytestmark = pytest.mark.usefixtures("assistant_master")
COUNT = "SELECT count(*) FROM assistant_pages WHERE bot_id = :bot"


async def test_with_or_without_md_is_the_same_page(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    await notes.write(db_session, bot.id, "人/李四", body="导入的一页", summary="")
    page = await notes.read(db_session, bot.id, "人/李四.md")
    assert page is not None and (page.path, page.body) == ("人/李四", "导入的一页")
    # 另一种写法当新页建：撞上已有的那页，不会多出第二页。
    clash = await refused(notes.write(db_session, bot.id, "人/李四.md", body="x", summary=""))
    assert clash.payload() == {"error": "page_exists", "current_version": 1}
    # 带着版本改：改的就是那一页，路径保持原来的写法。
    entry = await notes.write(db_session, bot.id, "人/李四.MD", body="改过", summary="", version=1)
    assert (entry.path, entry.version) == ("人/李四", 2)
    assert await notes.author(db_session, bot.id, "人/李四.md") == "assistant"
    assert await notes.delete(db_session, bot.id, "人/李四.md")
    assert await raw(db_session, COUNT, bot=bot.id) == 0


async def test_a_page_written_with_md_is_found_without_it(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    await notes.write(db_session, bot.id, "people/li.md", body="写的时候带 .md", summary="")
    page = await notes.read(db_session, bot.id, "People/Li")
    assert page is not None and page.path == "people/li.md"


async def test_links_find_a_page_by_its_name(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    await notes.write(db_session, bot.id, "人/李四.md", body="采购", summary="")
    await notes.write(db_session, bot.id, "项目/报价/季度报价.md", body="报价", summary="")
    for ref in ("[[李四]]", "[[李四|老李]]", "[[李四#近况]]", "![[李四]]", "李四", "[[人/李四]]"):
        page, candidates = await notes.lookup(db_session, bot.id, ref)
        assert page is not None and page.path == "人/李四.md" and candidates == [], ref
    page, _ = await notes.lookup(db_session, bot.id, "[[季度报价.md]]")
    assert page is not None and page.path == "项目/报价/季度报价.md"
    assert await notes.lookup(db_session, bot.id, "[[王五]]") == (None, [])


async def test_a_name_shared_by_several_pages_gives_candidates(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    await notes.write(db_session, bot.id, "人/李四.md", body="同事", summary="")
    await notes.write(db_session, bot.id, "客户/李四", body="客户", summary="")
    page, candidates = await notes.lookup(db_session, bot.id, "[[李四]]")
    assert page is None and candidates == ["人/李四.md", "客户/李四"]
    # 写全路径照样读得到。
    page, _ = await notes.lookup(db_session, bot.id, "[[客户/李四]]")
    assert page is not None and page.body == "客户"


async def test_edit_replaces_exactly_one_passage(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    body = "---\nstatus: 进行中\n---\n# 报价\n\n- 11-19 定价初稿\n- 11-18 立项\n"
    await notes.write(db_session, bot.id, "projects/q4.md", body=body, summary="季度报价")
    entry = await notes.edit(
        db_session,
        bot.id,
        "projects/q4",
        old="\n- 11-19 定价初稿",
        new="\n- 11-20 定价已确认\n- 11-19 定价初稿",
    )
    assert entry.version == 2
    page = await notes.read(db_session, bot.id, "projects/q4.md")
    assert page is not None and page.summary == "季度报价"
    assert page.body == body.replace("- 11-19", "- 11-20 定价已确认\n- 11-19")
    await notes.edit(
        db_session,
        bot.id,
        "projects/q4.md",
        old="status: 进行中",
        new="status: 已完成",
        summary="完",
    )
    page = await notes.read(db_session, bot.id, "projects/q4.md")
    assert page is not None and "status: 已完成" in page.body and page.summary == "完"
    for old, code in (
        ("不存在的一句", "edit_not_found"),
        ("- 11-1", "edit_not_unique"),
        ("", "invalid_edit"),
    ):
        error = await refused(notes.edit(db_session, bot.id, "projects/q4.md", old=old, new="x"))
        assert error.code == code
    missing = await refused(notes.edit(db_session, bot.id, "projects/none", old="a", new="b"))
    assert missing.code == "page_not_found"


async def test_edit_keeps_the_page_size_limit(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, _ = await assistant(db_session)
    await notes.write(db_session, bot.id, "a.md", body="短", summary="")
    monkeypatch.setattr(notes, "MAX_PAGE_BYTES", 10)
    error = await refused(notes.edit(db_session, bot.id, "a.md", old="短", new="长" * 10))
    assert error.code == "page_too_large"


async def test_tools_read_links_and_edit(db_session: AsyncSession) -> None:
    _, _, owner = await assistant_turn(db_session)
    await call(db_session, owner, "notes_write", path="人/李四.md", body="负责采购", summary="")
    out = await call(db_session, owner, "notes_read", path="[[李四]]")
    assert (out["path"], out["body"]) == ("人/李四.md", "负责采购")
    await call(db_session, owner, "notes_write", path="客户/李四.md", body="客户", summary="")
    assert await call(db_session, owner, "notes_read", path="[[李四]]") == {
        "error": "ambiguous_name",
        "candidates": ["人/李四.md", "客户/李四.md"],
    }
    edited = await call(
        db_session, owner, "notes_edit", path="人/李四.md", old="采购", new="采购与报价"
    )
    assert edited == {"status": "saved", "path": "人/李四.md", "version": 2, "pinned": False}
    assert (await call(db_session, owner, "notes_read", path="人/李四"))["body"] == "负责采购与报价"
    assert await call(db_session, owner, "notes_edit", path="人/李四", old="没有", new="x") == {
        "error": "edit_not_found"
    }


async def test_nobody_present_cannot_edit_a_pinned_page(db_session: AsyncSession) -> None:
    _, _, owner = await assistant_turn(db_session)
    await call(
        db_session,
        owner,
        "notes_write",
        path="rules/tone.md",
        body="先给结论",
        summary="",
        pinned=True,
    )
    unattended = replace(owner, event=None, platform=True)
    out = await call(db_session, unattended, "notes_edit", path="rules/tone.md", old="先", new="后")
    assert out == {"error": "pin_requires_owner", "hint": notes_tools.PIN_HINT}
    assert (await call(db_session, owner, "notes_read", path="rules/tone.md"))["body"] == "先给结论"


async def _imported(db_session: AsyncSession, bot: Bot, *, overwrite: bool) -> transfer.Plan:
    files = [("人/李四.md", "---\ntitle: 李四\nrole: 采购\n---\n新的正文\n")]
    parsed = archive.parse(archive.build(files, None))
    return await transfer.plan(
        db_session, bot.id, parsed, overwrite=overwrite, today=date(2026, 11, 20), fingerprint="f"
    )


async def test_an_import_matches_pages_with_or_without_md(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    await notes.write(db_session, bot.id, "人/李四.md", body="旧的", summary="")
    await db_session.commit()
    skipped = await _imported(db_session, bot, overwrite=False)
    assert skipped.create == [] and [item.reason for item in skipped.skip] == ["exists"]
    planned = await _imported(db_session, bot, overwrite=True)
    await transfer.apply(db_session, bot.id, planned, today=date(2026, 11, 20))
    assert await raw(db_session, COUNT, bot=bot.id) == 1
    page = await notes.read(db_session, bot.id, "人/李四")
    assert page is not None and page.path == "人/李四.md"
    # 其余 front matter 键留在正文开头。
    assert page.body == "---\nrole: 采购\n---\n新的正文\n"

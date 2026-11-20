"""知识库的双链：反链、孤立页与断链，改名时别的页里的链接跟着改。"""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import notes, notes_tools
from tests.integration.approval_helpers import assistant_turn
from tests.integration.test_assistant_notes import assistant, refused
from tests.integration.test_assistant_notes_tools import call

pytestmark = pytest.mark.usefixtures("assistant_master")


async def _body(db_session: AsyncSession, bot_id: object, path: str) -> str:
    page = await notes.read(db_session, bot_id, path)  # type: ignore[arg-type]
    assert page is not None
    return page.body


async def test_backlinks_and_outgoing_links(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    await notes.write(db_session, bot.id, "人/李四.md", body="负责 [[季度报价]]", summary="")
    await notes.write(
        db_session,
        bot.id,
        "项目/季度报价.md",
        body="负责人 [[李四|老李]]，见 [[决策/D-001 定价#原因]] 和 [[王五]]\n`[[代码里的]]`",
        summary="",
    )
    await notes.write(db_session, bot.id, "决策/D-001 定价.md", body="定了", summary="")
    await notes.write(
        db_session, bot.id, "日志/11-20.md", body="今天 ![[李四]] [[#小节]]", summary=""
    )
    found = await notes.links(db_session, bot.id, "人/李四")
    assert found == notes.Links(
        path="人/李四.md",
        linked_from=["日志/11-20.md", "项目/季度报价.md"],
        links_to=["项目/季度报价.md"],
        unresolved=[],
    )
    found = await notes.links(db_session, bot.id, "项目/季度报价.md")
    assert found is not None
    assert found.links_to == ["人/李四.md", "决策/D-001 定价.md"]
    assert found.unresolved == ["王五"]
    assert await notes.links(db_session, bot.id, "没有") is None


async def test_report_lists_isolated_pages_and_broken_links(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    await notes.write(db_session, bot.id, "a.md", body="[[b]] [[不存在]]", summary="")
    await notes.write(db_session, bot.id, "b.md", body="被链到", summary="")
    await notes.write(db_session, bot.id, "孤立.md", body="谁也不理", summary="")
    await notes.write(db_session, bot.id, "x/同名.md", body="一", summary="")
    await notes.write(db_session, bot.id, "y/同名.md", body="二 [[同名]]", summary="")
    found = await notes.report(db_session, bot.id)
    # y 里的 [[同名]] 有两页可指，不算链上：x、y 都是孤立页。
    assert found.orphans == ["x/同名.md", "y/同名.md", "孤立.md"]
    assert found.broken == [("a.md", "不存在"), ("y/同名.md", "同名")]
    assert (found.orphans_total, found.broken_total) == (3, 2)


async def test_rename_rewrites_links_elsewhere(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    await notes.write(db_session, bot.id, "人/李四.md", body="采购 [[人/李四]]", summary="")
    await notes.write(
        db_session,
        bot.id,
        "项目/报价.md",
        body="找 [[李四|老李]]、[[人/李四#近况]]、![[李四]]，`[[李四]]` 不动，[[王五]] 不动",
        summary="",
    )
    entry, relinked = await notes.rename(db_session, bot.id, "人/李四", "人/李四-采购.md")
    assert (entry.path, entry.title, entry.version) == ("人/李四-采购.md", "李四-采购", 2)
    assert relinked == ["项目/报价.md"]
    assert await _body(db_session, bot.id, "项目/报价") == (
        "找 [[李四-采购|老李]]、[[人/李四-采购#近况]]、![[李四-采购]]，"
        "`[[李四]]` 不动，[[王五]] 不动"
    )
    # 自己页里的自链也跟着改；旧路径没有了。
    assert await _body(db_session, bot.id, "人/李四-采购") == "采购 [[人/李四-采购]]"
    assert await notes.read(db_session, bot.id, "人/李四") is None
    page = await notes.read(db_session, bot.id, "项目/报价")
    assert page is not None and page.version == 2


async def test_rename_keeps_links_pointing_at_the_page_they_meant(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    await notes.write(db_session, bot.id, "客户/李四.md", body="客户", summary="")
    await notes.write(db_session, bot.id, "草稿/张三.md", body="同事", summary="")
    await notes.write(db_session, bot.id, "项目.md", body="[[李四]] 和 [[张三]]", summary="")
    # 改完有两页叫「李四」：原来指向客户的那条写成全路径，指向改名页的也写全路径。
    await notes.rename(db_session, bot.id, "草稿/张三.md", "人/李四.md")
    assert await _body(db_session, bot.id, "项目.md") == "[[客户/李四]] 和 [[人/李四]]"


async def test_rename_refusals(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    await notes.write(db_session, bot.id, "a.md", body="一", summary="", title="自定标题")
    await notes.write(db_session, bot.id, "b.md", body="二", summary="")
    assert (await refused(notes.rename(db_session, bot.id, "none", "c"))).code == "page_not_found"
    clash = await refused(notes.rename(db_session, bot.id, "a", "B"))
    assert clash.payload() == {"error": "page_exists", "current_version": 1}
    bad = await refused(notes.rename(db_session, bot.id, "a", "../x"))
    assert bad.code == "invalid_path"
    # 只换大小写、加上 .md 都可以；自定的标题不动。
    entry, _ = await notes.rename(db_session, bot.id, "a", "A")
    assert (entry.path, entry.title) == ("A", "自定标题")
    entry, _ = await notes.rename(db_session, bot.id, "a", "a.md")
    assert entry.path == "a.md" and await notes.read(db_session, bot.id, "A") is not None


async def test_rename_keeps_the_size_limit(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, _ = await assistant(db_session)
    await notes.write(db_session, bot.id, "a.md", body="[[b]]", summary="")
    await notes.write(db_session, bot.id, "b.md", body="二", summary="")
    monkeypatch.setattr(notes, "MAX_PAGE_BYTES", 6)
    error = await refused(notes.rename(db_session, bot.id, "b", "很长的新名字"))
    assert error.code == "page_too_large"


async def test_tools_rename_and_links(db_session: AsyncSession) -> None:
    _, _, owner = await assistant_turn(db_session)
    await call(db_session, owner, "notes_write", path="人/李四.md", body="采购", summary="")
    await call(db_session, owner, "notes_write", path="项目.md", body="[[李四]]", summary="")
    out = await call(db_session, owner, "notes_links", path="[[李四]]")
    assert out == {
        "path": "人/李四.md",
        "linked_from": ["项目.md"],
        "links_to": [],
        "unresolved": [],
    }
    renamed = await call(db_session, owner, "notes_rename", path="人/李四", new_path="人/老李.md")
    assert renamed == {
        "status": "renamed",
        "path": "人/老李.md",
        "version": 2,
        "links_updated_in": ["项目.md"],
    }
    assert (await call(db_session, owner, "notes_read", path="项目"))["body"] == "[[老李]]"
    report = await call(db_session, owner, "notes_links")
    assert report == {"orphans": [], "orphans_total": 0, "broken": [], "broken_total": 0}
    assert await call(db_session, owner, "notes_links", path="没有") == {"error": "page_not_found"}


async def test_nobody_present_cannot_rename_into_a_pinned_page(db_session: AsyncSession) -> None:
    _, _, owner = await assistant_turn(db_session)
    await call(db_session, owner, "notes_write", path="a.md", body="一", summary="")
    await call(
        db_session, owner, "notes_write", path="rules.md", body="[[a]]", summary="", pinned=True
    )
    unattended = replace(owner, event=None, platform=True)
    out = await call(db_session, unattended, "notes_rename", path="a", new_path="b")
    assert out == {"error": "pin_requires_owner", "hint": notes_tools.PIN_HINT}
    # 没有置顶页牵连时照样能改。
    await call(db_session, owner, "notes_write", path="c.md", body="三", summary="")
    out = await call(db_session, unattended, "notes_rename", path="c", new_path="d")
    assert out["status"] == "renamed"

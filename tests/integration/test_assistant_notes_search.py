"""知识库的列出与检索：只看得到自己的页；检索的排序与片段。"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import keys, notes, sealed
from coreman.core.db.models import Bot, User
from tests.integration.assistant_helpers import seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")


async def assistant(session: AsyncSession) -> tuple[Bot, User]:
    bot, owner = await seed_assistant(session)
    await keys.ensure_key(session, sealed.master(), owner.id)
    await session.commit()
    return bot, owner


async def library(session: AsyncSession, bot: Bot) -> None:
    """四页：报价策略、李四、一天的日志、一页无关的。"""
    await notes.write(
        session,
        bot.id,
        "projects/pricing.md",
        title="报价策略",
        summary="Q4 报价怎么定",
        body="先看成本。报价不低于成本价；报价单发出前给上级过目。折扣最多九折。",
        pinned=True,
    )
    await notes.write(
        session,
        bot.id,
        "People/Li.md",
        title="李四",
        summary="负责报价的同事",
        body="他常来问折扣能给到多少。",
    )
    await notes.write(
        session,
        bot.id,
        "log/2026-11-12.md",
        title="日志",
        summary="当天记录",
        body="上午：" + "报价 " * 9 + "都回了。",
    )
    await notes.write(
        session, bot.id, "misc/lunch.md", title="午饭", summary="楼下的面馆", body="周三不开门。"
    )
    await session.commit()


async def test_index_lists_my_pages_in_path_order(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    other, _ = await assistant(db_session)
    await library(db_session, bot)
    await notes.write(db_session, other.id, "secret/plan.md", body="别人的页", summary="别人的")
    await db_session.commit()
    entries = await notes.index(db_session, bot.id)
    assert [entry.path for entry in entries] == [
        "log/2026-11-12.md",
        "misc/lunch.md",
        "People/Li.md",
        "projects/pricing.md",
    ]
    li = entries[2]
    assert (li.title, li.summary, li.pinned, li.version) == ("李四", "负责报价的同事", False, 1)
    assert entries[3].pinned is True and not hasattr(entries[3], "body")
    assert [entry.path for entry in await notes.index(db_session, other.id)] == ["secret/plan.md"]


async def test_index_can_be_limited_to_a_folder(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    await library(db_session, bot)
    await notes.write(db_session, bot.id, "people-old/wang.md", body="已离职", summary="")
    await db_session.commit()
    for folder in ("people", "/PEOPLE/", "people\\"):
        assert [e.path for e in await notes.index(db_session, bot.id, folder=folder)] == [
            "People/Li.md"
        ]
    assert await notes.index(db_session, bot.id, folder="nowhere") == []
    assert len(await notes.index(db_session, bot.id, folder=" / ")) == 5


async def test_search_ranks_by_terms_matched_then_score_then_path(
    db_session: AsyncSession,
) -> None:
    bot, _ = await assistant(db_session)
    await library(db_session, bot)
    # 一个词：标题 8 + 摘要 3 + 正文 2 次 = 13；日志正文 9 次封顶 5；李四只有摘要 3。
    hits = await notes.search(db_session, bot.id, "报价")
    assert [(hit.path, hit.score) for hit in hits] == [
        ("projects/pricing.md", 13),
        ("log/2026-11-12.md", 5),
        ("People/Li.md", 3),
    ]
    # 两个词：命中两个词的排在只命中一个的前面，哪怕后者分数更高（李四 4 分，日志 5 分）。
    hits = await notes.search(db_session, bot.id, "报价 折扣")
    assert [(hit.path, hit.score) for hit in hits] == [
        ("projects/pricing.md", 14),
        ("People/Li.md", 4),
        ("log/2026-11-12.md", 5),
    ]
    assert hits[0].title == "报价策略" and hits[0].pinned is True and hits[0].version == 1


async def test_search_breaks_ties_by_path_and_ignores_case(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    for path in ("b/second.md", "a/first.md", "C/Third.md"):
        await notes.write(
            db_session, bot.id, path, title="页", summary="", body="提到 Alpha 一次。"
        )
    await db_session.commit()
    hits = await notes.search(db_session, bot.id, "ALPHA")
    assert [(hit.path, hit.score) for hit in hits] == [
        ("a/first.md", 1),
        ("b/second.md", 1),
        ("C/Third.md", 1),
    ]
    # 路径也算：5 分。
    assert [(h.path, h.score) for h in await notes.search(db_session, bot.id, "third")] == [
        ("C/Third.md", 5)
    ]
    assert len(await notes.search(db_session, bot.id, "alpha", limit=2)) == 2


async def test_search_returns_a_one_line_snippet_around_the_hit(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    body = "开头。" + "铺垫" * 40 + "\n关键的一句：周四前给报价。\n" + "收尾" * 100
    await notes.write(db_session, bot.id, "a.md", title="报价跟进", summary="甲的摘要", body=body)
    await notes.write(
        db_session, bot.id, "b.md", title="乙", summary="只在摘要里提到报价", body="无关。"
    )
    await db_session.commit()
    first, second = await notes.search(db_session, bot.id, "报价")
    assert first.path == "a.md"
    assert "关键的一句：周四前给报价。" in first.snippet and "\n" not in first.snippet
    assert first.snippet.startswith("…") and first.snippet.endswith("…")
    assert "开头。" not in first.snippet
    assert len(first.snippet) <= notes.SNIPPET_BEFORE + notes.SNIPPET_AFTER + len("报价") + 2
    # 正文没命中：片段用摘要。
    assert second.path == "b.md" and second.snippet == "只在摘要里提到报价"


async def test_search_with_nothing_to_look_for_finds_nothing(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    other, _ = await assistant(db_session)
    await library(db_session, bot)
    assert await notes.search(db_session, bot.id, "   ") == []
    assert await notes.search(db_session, bot.id, "不存在的词") == []
    # 别的助理搜不到我的页。
    assert await notes.search(db_session, other.id, "报价") == []


async def test_locked_pages_are_neither_listed_nor_found(db_session: AsyncSession) -> None:
    bot, owner = await assistant(db_session)
    await library(db_session, bot)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    assert await notes.index(db_session, bot.id) == []
    assert await notes.search(db_session, bot.id, "报价") == []

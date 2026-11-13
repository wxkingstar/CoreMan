"""导入的清单与写入：新建 / 覆盖 / 跳过各归各位，出清单不写库，写入带着清单里的版本号。"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import archive, followups, keys, notes, sealed, transfer
from coreman.core.assistants.transfer import TransferError
from coreman.core.db.models import Bot
from tests.fakes.archives import pack
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")
TODAY = date(2026, 11, 13)
VOICE = "---\ntitle: 口吻\nsummary: 说话的规矩\npinned: true\n---\n回复要短。"
PAGES = "SELECT count(*) FROM assistant_pages WHERE bot_id = :bot"
FOLLOWUPS = "SELECT count(*) FROM assistant_followups WHERE bot_id = :bot"
AUTHORS = "SELECT string_agg(DISTINCT updated_by, ',') FROM assistant_pages WHERE bot_id = :bot"
STORED = (
    "SELECT string_agg(path_enc || title_enc || summary_enc || body_enc, chr(32))"
    " FROM assistant_pages WHERE bot_id = :bot"
)


async def _owner(db_session: AsyncSession) -> Bot:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    await db_session.commit()
    return bot


def _parsed(pages: dict[str, str], items: list[dict[str, Any]] | None = None) -> archive.Parsed:
    listed = archive.followups_text(items) if items is not None else None
    return archive.parse(archive.build(list(pages.items()), listed))


async def _plan(
    db_session: AsyncSession,
    bot: Bot,
    parsed: archive.Parsed,
    *,
    overwrite: bool = False,
    fingerprint: str = "f",
) -> transfer.Plan:
    return await transfer.plan(
        db_session, bot.id, parsed, overwrite=overwrite, today=TODAY, fingerprint=fingerprint
    )


async def test_planning_sorts_everything_and_writes_nothing(db_session: AsyncSession) -> None:
    bot = await _owner(db_session)
    await notes.write(db_session, bot.id, "规则/口吻", body="旧的", summary="旧摘要", pinned=True)
    await followups.add(db_session, bot.id, category="waiting", title="等报价")
    await db_session.commit()
    parsed = _parsed(
        {
            "规则/口吻": "新的正文",
            "人/李四": "---\ntitle: 李四\nsummary: 采购  负责人\npinned: true\n---\n正文",
            "人/李四 ": "规范化之后和上一个是同一个路径",
            "a/b/c/d/e/f/g/h/i": "层数太多",
            "空": "   ",
            "坏头": "---\ntitle: [1]\n---\nx",
            "大": "字" * 70_000,
            "长摘要": "---\nsummary: " + "长" * 301 + "\n---\nx",
        },
        [
            {"category": "waiting", "title": "等报价"},
            {"category": "remind", "title": "体检"},
            {"category": "nope", "title": "x"},
        ],
    )
    planned = await _plan(db_session, bot, parsed)
    assert transfer.preview(planned) == {
        "digest": planned.digest,
        "ignored": 0,
        "create": [{"path": "人/李四", "pinned": True}],
        "overwrite": [],
        "skip": [
            {"path": "规则/口吻", "reason": "exists"},
            {"path": "人/李四", "reason": "duplicate_path"},
            {"path": "a/b/c/d/e/f/g/h/i", "reason": "invalid_path"},
            {"path": "空", "reason": "empty_page"},
            {"path": "坏头", "reason": "invalid_front_matter"},
            {"path": "大", "reason": "page_too_large"},
            {"path": "长摘要", "reason": "invalid_page"},
        ],
        "followups": {
            "create": 1,
            "skip": [
                {"index": 1, "reason": "duplicate"},
                {"index": 3, "reason": "invalid_category"},
            ],
        },
    }
    assert len(planned.digest) == 64
    assert transfer.counts(planned) == "new=1,over=0,skip=7,fu=1"
    # 出清单不写任何页面与待跟进。
    await db_session.commit()
    assert await raw(db_session, PAGES, bot=bot.id) == 1
    assert await raw(db_session, FOLLOWUPS, bot=bot.id) == 1


async def test_ignored_system_files_are_counted_in_the_preview(db_session: AsyncSession) -> None:
    bot = await _owner(db_session)
    parsed = archive.parse(
        pack({"好.md": "正文", ".DS_Store": b"\x00", "__MACOSX/._好.md": b"\x00\x05"})
    )
    planned = await _plan(db_session, bot, parsed)
    shown = transfer.preview(planned)
    assert shown["ignored"] == 2 and shown["create"] == [{"path": "好", "pinned": False}]
    assert shown["skip"] == []
    # 访问记录里的对象还是那四个数：忽略的文件没有读，不算。
    assert transfer.counts(planned) == "new=1,over=0,skip=0,fu=0"


async def test_applying_creates_pages_and_followups_as_planned(db_session: AsyncSession) -> None:
    bot = await _owner(db_session)
    parsed = _parsed(
        {"规则/口吻": VOICE + SECRET, "人/李四": "采购负责人。"},
        [
            {"category": "waiting", "title": "等报价", "notes": "催过一次", "due_on": "2026-12-01"},
            {"category": "remind", "title": "体检", "review_on": "2099-01-01", "status": "snoozed"},
            {
                "category": "remind",
                "title": "早该看了",
                "review_on": "2020-01-01",
                "status": "snoozed",
            },
        ],
    )
    planned = await _plan(db_session, bot, parsed)
    await transfer.apply(db_session, bot.id, planned, today=TODAY)
    await db_session.commit()
    voice = await notes.read(db_session, bot.id, "规则/口吻")
    assert voice is not None and voice.body == f"回复要短。{SECRET}"
    # front matter 里写了置顶：导入是所有者本人在场的操作，照办。
    assert (voice.title, voice.summary, voice.pinned, voice.version) == (
        "口吻",
        "说话的规矩",
        True,
        1,
    )
    other = await notes.read(db_session, bot.id, "人/李四")
    assert other is not None
    assert (other.title, other.summary, other.pinned) == ("李四", "", False)
    assert await raw(db_session, AUTHORS, bot=bot.id) == "import"
    assert SECRET not in str(await raw(db_session, STORED, bot=bot.id))
    rows = await followups.list_(db_session, bot.id, "open", today=TODAY)
    assert [
        (row.category, row.title_enc, row.notes_enc, row.review_on, row.due_on, row.status)
        for row in rows
    ] == [
        ("waiting", "等报价", "催过一次", None, date(2026, 12, 1), "open"),
        ("remind", "体检", None, date(2099, 1, 1), None, "snoozed"),
        # 回看日已经过了的推后条目，导进来就是未结。
        ("remind", "早该看了", None, date(2020, 1, 1), None, "open"),
    ]


async def test_overwriting_keeps_what_the_file_does_not_say(db_session: AsyncSession) -> None:
    bot = await _owner(db_session)
    await notes.write(
        db_session, bot.id, "规则/口吻", body="旧的", summary="旧摘要", title="口吻", pinned=True
    )
    await db_session.commit()
    planned = await _plan(db_session, bot, _parsed({"规则/口吻": "第二版"}), overwrite=True)
    assert transfer.preview(planned)["overwrite"] == [{"path": "规则/口吻", "pinned": True}]
    assert transfer.preview(planned)["create"] == []
    await transfer.apply(db_session, bot.id, planned, today=TODAY)
    await db_session.commit()
    page = await notes.read(db_session, bot.id, "规则/口吻")
    assert page is not None
    assert (page.body, page.summary, page.title, page.pinned, page.version) == (
        "第二版",
        "旧摘要",
        "口吻",
        True,
        2,
    )
    assert await raw(db_session, AUTHORS, bot=bot.id) == "import"
    # 文件里写明了的，照文件的来。
    explicit = _parsed({"规则/口吻": "---\nsummary: 新摘要\npinned: false\n---\n第三版"})
    planned = await _plan(db_session, bot, explicit, overwrite=True)
    await transfer.apply(db_session, bot.id, planned, today=TODAY)
    await db_session.commit()
    page = await notes.read(db_session, bot.id, "规则/口吻")
    assert page is not None
    assert (page.body, page.summary, page.title, page.pinned, page.version) == (
        "第三版",
        "新摘要",
        "口吻",
        False,
        3,
    )


async def test_the_digest_changes_with_anything_the_owner_confirmed(
    db_session: AsyncSession,
) -> None:
    bot = await _owner(db_session)
    await notes.write(db_session, bot.id, "规则/口吻", body="旧的", summary="")
    await db_session.commit()
    parsed = _parsed({"规则/口吻": "新的", "人/李四": "x"})
    base = await _plan(db_session, bot, parsed, overwrite=True)
    assert (await _plan(db_session, bot, parsed, overwrite=True)).digest == base.digest
    assert (await _plan(db_session, bot, parsed, overwrite=False)).digest != base.digest
    changed = await _plan(db_session, bot, parsed, overwrite=True, fingerprint="another archive")
    assert changed.digest != base.digest
    # 助理在预览与确认之间改了要覆盖的那一页：版本号变了，摘要跟着变。
    await notes.write(db_session, bot.id, "规则/口吻", body="助理改的", summary="", version=1)
    await db_session.commit()
    assert (await _plan(db_session, bot, parsed, overwrite=True)).digest != base.digest


async def test_a_plan_made_before_a_page_appeared_is_refused(db_session: AsyncSession) -> None:
    bot = await _owner(db_session)
    # 回滚会让会话里的对象过期：之后还要用的编号先取出来。
    bot_id = bot.id
    planned = await _plan(db_session, bot, _parsed({"人/李四": "包里的", "规则/口吻": "包里的"}))
    await notes.write(db_session, bot_id, "规则/口吻", body="助理先写了", summary="")
    await db_session.commit()
    with pytest.raises(TransferError) as refused:
        await transfer.apply(db_session, bot_id, planned, today=TODAY)
    assert refused.value.code == "plan_changed"
    # 调用方回滚：前面已经写下的那一页一并作废。
    await db_session.rollback()
    assert await notes.read(db_session, bot_id, "人/李四") is None
    page = await notes.read(db_session, bot_id, "规则/口吻")
    assert page is not None and page.body == "助理先写了"


@pytest.mark.parametrize(
    ("module", "name", "value", "reason"),
    [
        (notes, "MAX_PAGES", 1, "too_many_pages"),
        (notes, "MAX_TOTAL_BYTES", 5, "notes_full"),
        (followups, "MAX_OPEN", 1, "too_many_followups"),
    ],
)
async def test_limits_refuse_the_whole_import(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    name: str,
    value: int,
    reason: str,
) -> None:
    bot = await _owner(db_session)
    parsed = _parsed(
        {"a": "正文一", "b": "正文二"},
        [{"category": "remind", "title": "一"}, {"category": "remind", "title": "二"}],
    )
    monkeypatch.setattr(module, name, value)
    with pytest.raises(TransferError) as refused:
        await _plan(db_session, bot, parsed)
    assert refused.value.code == reason


async def test_a_destroyed_key_refuses_before_anything_is_read(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    with pytest.raises(TransferError) as refused:
        await _plan(db_session, bot, _parsed({"a": "x"}))
    assert refused.value.code == "content_locked"

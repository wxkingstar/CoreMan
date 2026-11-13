"""知识库的写入、读取与删除：库里是密文，按助理隔离，路径唯一，乐观锁，三个上限。"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, notes, sealed
from coreman.core.crypto import DecryptError
from coreman.core.db.models import Bot, User
from coreman.core.db.session import make_session_factory
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
STORED = (
    "SELECT string_agg(path_hmac || path_enc || title_enc || summary_enc || body_enc, '|')"
    " FROM assistant_pages WHERE bot_id = :bot"
)
COUNT = "SELECT count(*) FROM assistant_pages WHERE bot_id = :bot"


async def assistant(session: AsyncSession) -> tuple[Bot, User]:
    bot, owner = await seed_assistant(session)
    await keys.ensure_key(session, sealed.master(), owner.id)
    await session.commit()
    return bot, owner


async def refused(coro: object) -> notes.NotesError:
    with pytest.raises(notes.NotesError) as caught:
        await coro  # type: ignore[misc]
    return caught.value


async def test_page_is_stored_sealed_and_reads_back(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    body = f"李四负责报价。{SECRET}"
    entry = await notes.write(
        db_session, bot.id, f"people/{SECRET}.md", body=body, summary=f"李四 {SECRET}"
    )
    await db_session.commit()
    assert entry.version == 1 and entry.pinned is False
    assert entry.title == SECRET and entry.size == len(body.encode("utf-8"))
    stored = str(await raw(db_session, STORED, bot=bot.id))
    assert SECRET not in stored and "people" not in stored and "李四" not in stored
    assert stored.count("enc:v2:d1:") == 4
    page = await notes.read(db_session, bot.id, f"people/{SECRET}.md")
    assert page is not None and page.body == body and page.summary == f"李四 {SECRET}"
    assert page.path == f"people/{SECRET}.md" and page.version == 1
    assert page.updated_at == entry.updated_at
    assert await notes.read(db_session, bot.id, "people/nobody.md") is None


async def test_other_assistants_cannot_read_or_collide(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    mine, _ = await assistant(db_session)
    theirs, _ = await assistant(db_session)
    await notes.write(
        db_session, mine.id, "people/boss.md", body="我的上级是王五。", summary="上级"
    )
    await db_session.commit()
    assert await notes.read(db_session, theirs.id, "people/boss.md") is None
    # 同一个路径在另一位助理那里是另一页，互不影响。
    other = await notes.write(
        db_session, theirs.id, "people/boss.md", body="另一个人的上级。", summary="上级"
    )
    await db_session.commit()
    assert other.version == 1
    page = await notes.read(db_session, mine.id, "people/boss.md")
    assert page is not None and page.body == "我的上级是王五。"
    # 同一个路径在两位助理那里的摘要不同：只拿到数据库的人对不出「他们有同一页」。
    hmacs = "SELECT count(DISTINCT path_hmac) FROM assistant_pages"
    assert await raw(db_session, hmacs) == 2
    # 把我的那一行原样挪到对方名下（连路径摘要也改成对方的）：密文绑着助理，解不开。
    assert await notes.delete(db_session, theirs.id, "people/boss.md")
    mac = await sealed.content_mac(db_session, theirs.id, notes.PATH_PURPOSE, "people/boss.md")
    await raw(
        db_session,
        "UPDATE assistant_pages SET bot_id = :theirs, path_hmac = :mac WHERE bot_id = :mine"
        " RETURNING 1",
        theirs=theirs.id,
        mine=mine.id,
        mac=mac,
    )
    await db_session.commit()
    async with make_session_factory(db_engine)() as fresh:
        with pytest.raises(DecryptError):
            await notes.read(fresh, theirs.id, "people/boss.md")


async def test_path_is_unique_after_normalizing(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    await notes.write(db_session, bot.id, "People/Boss.md", body="第一版", summary="上级")
    await db_session.commit()
    for same in (" /people//boss.md/ ", "PEOPLE\\BOSS.MD", "people/boss.md"):
        error = await refused(notes.write(db_session, bot.id, same, body="另一页", summary="x"))
        assert error.payload() == {"error": "page_exists", "current_version": 1}
    entry = await notes.write(
        db_session, bot.id, "PEOPLE/BOSS.MD", body="第二版", summary="上级", version=1
    )
    await db_session.commit()
    assert entry.version == 2 and entry.path == "People/Boss.md"
    assert await raw(db_session, COUNT, bot=bot.id) == 1
    page = await notes.read(db_session, bot.id, "/people/boss.md/")
    assert page is not None and page.body == "第二版" and page.path == "People/Boss.md"


async def test_version_is_an_optimistic_lock(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    path = "projects/pricing.md"
    missing = await refused(notes.write(db_session, bot.id, path, body="x", summary="", version=3))
    assert missing.payload() == {"error": "page_not_found"}
    await notes.write(db_session, bot.id, path, body="v1", summary="报价", title="报价策略")
    second = await notes.write(
        db_session, bot.id, path, body="v2", summary="报价", pinned=True, version=1
    )
    await db_session.commit()
    # 省略 title 时保持原标题；pinned 给了就改。
    assert second.version == 2 and second.title == "报价策略" and second.pinned is True
    # 另一个写的人还拿着版本 1。
    stale = await refused(
        notes.write(db_session, bot.id, path, body="晚到的", summary="", version=1)
    )
    assert stale.payload() == {"error": "version_conflict", "current_version": 2}
    page = await notes.read(db_session, bot.id, path)
    assert page is not None and page.body == "v2" and page.version == 2
    # 省略 pinned 时保持原样。
    third = await notes.write(db_session, bot.id, path, body="v3", summary="报价", version=2)
    assert third.version == 3 and third.pinned is True


async def test_single_page_limit_counts_bytes(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    assert notes.MAX_PAGE_BYTES == 200 * 1024
    fits = await notes.write(
        db_session, bot.id, "log/big.md", body="x" * notes.MAX_PAGE_BYTES, summary="大页"
    )
    assert fits.size == notes.MAX_PAGE_BYTES
    too_long = await refused(
        notes.write(
            db_session, bot.id, "log/over.md", body="x" * (notes.MAX_PAGE_BYTES + 1), summary=""
        )
    )
    assert too_long.payload() == {
        "error": "page_too_large",
        "limit_bytes": notes.MAX_PAGE_BYTES,
        "size_bytes": notes.MAX_PAGE_BYTES + 1,
    }
    # 按字节不按字符：一个汉字三个字节。
    wide = "中" * (notes.MAX_PAGE_BYTES // 3 + 1)
    assert (
        await refused(notes.write(db_session, bot.id, "log/w.md", body=wide, summary=""))
    ).code == ("page_too_large")


async def test_page_count_limit(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    assert notes.MAX_PAGES == 2000
    monkeypatch.setattr(notes, "MAX_PAGES", 2)
    bot, _ = await assistant(db_session)
    other, _ = await assistant(db_session)
    await notes.write(db_session, bot.id, "a.md", body="1", summary="")
    await notes.write(db_session, bot.id, "b.md", body="2", summary="")
    full = await refused(notes.write(db_session, bot.id, "c.md", body="3", summary=""))
    assert full.payload() == {"error": "too_many_pages", "limit": 2}
    # 改已有的页不占新名额；别的助理各算各的；删掉一页就又能建。
    assert (
        await notes.write(db_session, bot.id, "a.md", body="1b", summary="", version=1)
    ).version == 2
    assert (await notes.write(db_session, other.id, "c.md", body="3", summary="")).version == 1
    assert await notes.delete(db_session, bot.id, "b.md")
    assert (await notes.write(db_session, bot.id, "c.md", body="3", summary="")).version == 1


async def test_total_size_limit(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    assert notes.MAX_TOTAL_BYTES == 20 * 1024 * 1024
    monkeypatch.setattr(notes, "MAX_TOTAL_BYTES", 100)
    bot, _ = await assistant(db_session)
    await notes.write(db_session, bot.id, "a.md", body="x" * 60, summary="")
    full = await refused(notes.write(db_session, bot.id, "b.md", body="x" * 41, summary=""))
    assert full.payload() == {"error": "notes_full", "limit_bytes": 100}
    await notes.write(db_session, bot.id, "b.md", body="x" * 40, summary="")
    # 改写按差额算：60 → 61 就超了，60 → 30 之后又有余量。
    grow = await refused(
        notes.write(db_session, bot.id, "a.md", body="x" * 61, summary="", version=1)
    )
    assert grow.code == "notes_full"
    await notes.write(db_session, bot.id, "a.md", body="x" * 30, summary="", version=1)
    assert (await notes.write(db_session, bot.id, "c.md", body="x" * 30, summary="")).version == 1


async def test_delete_frees_the_path(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    await notes.write(db_session, bot.id, "tmp/draft.md", body="草稿", summary="")
    await db_session.commit()
    assert await notes.delete(db_session, bot.id, "TMP/Draft.md") is True
    assert await notes.delete(db_session, bot.id, "tmp/draft.md") is False
    await db_session.commit()
    assert await notes.read(db_session, bot.id, "tmp/draft.md") is None
    assert (
        await notes.write(db_session, bot.id, "tmp/draft.md", body="重写", summary="")
    ).version == 1


async def test_bad_pages_are_refused(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    for kw in (
        {"body": "  \n ", "summary": ""},
        {"body": "x", "summary": "摘" * (notes.MAX_SUMMARY_CHARS + 1)},
        {"body": "x", "summary": "", "title": "题" * (notes.MAX_TITLE_CHARS + 1)},
        {"body": "x", "summary": "", "updated_by": "someone"},
    ):
        error = await refused(notes.write(db_session, bot.id, "a.md", **kw))  # type: ignore[arg-type]
        assert error.payload() == {"error": "invalid_page"}
    assert (
        await refused(notes.write(db_session, bot.id, "a/../b", body="x", summary=""))
    ).code == ("invalid_path")
    assert await raw(db_session, COUNT, bot=bot.id) == 0


async def test_destroyed_key_locks_the_knowledge_base(db_session: AsyncSession) -> None:
    bot, owner = await assistant(db_session)
    await notes.write(db_session, bot.id, "a.md", body="内容", summary="")
    await db_session.commit()
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    assert (await refused(notes.read(db_session, bot.id, "a.md"))).code == "content_locked"
    assert (await refused(notes.write(db_session, bot.id, "b.md", body="x", summary=""))).code == (
        "content_locked"
    )
    assert (await refused(notes.delete(db_session, bot.id, "a.md"))).code == "content_locked"
    assert await raw(db_session, COUNT, bot=bot.id) == 1


async def test_standard_bots_have_no_knowledge_base(db_session: AsyncSession) -> None:
    bot = await seed_standard(db_session)
    error = await refused(notes.write(db_session, bot.id, "a.md", body="x", summary=""))
    assert error.payload() == {"error": "assistant_only"}
    assert (await refused(notes.read(db_session, bot.id, "a.md"))).code == "assistant_only"

"""导入接口：先预览后确认。预览不写页面；确认带回摘要才写；任何一步失败整体回滚。"""

from __future__ import annotations

import json
import stat
from datetime import date

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import archive, followups, keys, notes
from tests.api.conftest import login_as
from tests.api.my_assistant_helpers import URL, as_bot_token, as_cli, code, owned
from tests.fakes.archives import encrypted, pack, special
from tests.integration.assistant_helpers import SECRET, raw

pytestmark = pytest.mark.usefixtures("assistant_master")
IMPORT = f"{URL}/import"
TODAY = date(2026, 11, 13)
PAGES = "SELECT count(*) FROM assistant_pages WHERE bot_id = :bot"
FOLLOWUPS = "SELECT count(*) FROM assistant_followups WHERE bot_id = :bot"
AUTHORS = "SELECT string_agg(DISTINCT updated_by, ',') FROM assistant_pages WHERE bot_id = :bot"
SAMPLE = pack(
    {
        "规则/口吻.md": (
            f"---\ntitle: 口吻\nsummary: 说话的规矩\npinned: true\n---\n回复要短。{SECRET}\n"
        ),
        "人/李四.md": "采购负责人。\n",
        "followups.json": json.dumps(
            {
                "version": 1,
                "items": [{"category": "waiting", "title": "等报价", "due_on": "2026-12-01"}],
            },
            ensure_ascii=False,
        ),
    }
)


async def _post(client: httpx.AsyncClient, data: bytes, **fields: str) -> httpx.Response:
    return await client.post(
        IMPORT, files={"archive": ("notes.zip", data, "application/zip")}, data=fields
    )


async def _logged(db_session: AsyncSession) -> list[tuple[str, str]]:
    rows = await db_session.execute(
        text("SELECT action, target FROM assistant_access_log ORDER BY id")
    )
    return [(row[0], row[1]) for row in rows.all()]


async def test_a_preview_lists_what_would_happen_and_writes_no_page(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    response = await _post(client, SAMPLE)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    assert data["dry_run"] is True and len(data["digest"]) == 64
    assert data["create"] == [
        {"path": "规则/口吻", "pinned": True},
        {"path": "人/李四", "pinned": False},
    ]
    assert data["overwrite"] == [] and data["skip"] == []
    assert data["followups"] == {"create": 1, "skip": []}
    # 清单只有路径、原因与是否置顶：正文不回显。
    assert SECRET not in response.text
    assert await raw(db_session, PAGES, bot=bot.id) == 0
    assert await raw(db_session, FOLLOWUPS, bot=bot.id) == 0
    # 出清单读了知识库的目录：留一条访问记录，对象只有四个数。
    assert await _logged(db_session) == [("import.preview", "new=2,over=0,skip=0,fu=1")]


async def test_confirming_with_the_digest_writes_everything(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    digest = (await _post(client, SAMPLE)).json()["data"]["digest"]
    done = await _post(client, SAMPLE, dry_run="false", expect=digest)
    assert done.status_code == 200, done.text
    assert done.json()["data"]["dry_run"] is False and done.json()["data"]["digest"] == digest
    voice = await notes.read(db_session, bot.id, "规则/口吻")
    assert voice is not None and voice.body == f"回复要短。{SECRET}\n"
    # 置顶页确实置顶了；这是所有者本人在场的操作。
    assert (voice.title, voice.summary, voice.pinned, voice.version) == (
        "口吻",
        "说话的规矩",
        True,
        1,
    )
    other = await notes.read(db_session, bot.id, "人/李四")
    assert other is not None and (other.title, other.pinned) == ("李四", False)
    assert await raw(db_session, AUTHORS, bot=bot.id) == "import"
    [item] = await followups.list_(db_session, bot.id, "open", today=TODAY)
    assert (item.category, item.title_enc, item.due_on) == ("waiting", "等报价", date(2026, 12, 1))
    assert await _logged(db_session) == [
        ("import.preview", "new=2,over=0,skip=0,fu=1"),
        ("import.notes", "new=2,over=0,skip=0,fu=1"),
    ]


async def test_existing_pages_are_skipped_unless_overwrite_is_asked(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    digest = (await _post(client, SAMPLE)).json()["data"]["digest"]
    await _post(client, SAMPLE, dry_run="false", expect=digest)
    # 同一个包再来一次：页面都已存在，待跟进也已有同样的一条。
    again = (await _post(client, SAMPLE)).json()["data"]
    assert again["create"] == [] and again["overwrite"] == []
    assert again["skip"] == [
        {"path": "规则/口吻", "reason": "exists"},
        {"path": "人/李四", "reason": "exists"},
    ]
    assert again["followups"] == {"create": 0, "skip": [{"index": 1, "reason": "duplicate"}]}

    newer = pack({"规则/口吻.md": "第二版，没有写头。\n"})
    plan = (await _post(client, newer, overwrite="true")).json()["data"]
    # 文件没说置顶与否：保持原样（原来是置顶的），清单里如实标出。
    assert plan["overwrite"] == [{"path": "规则/口吻", "pinned": True}] and plan["skip"] == []
    done = await _post(client, newer, dry_run="false", overwrite="true", expect=plan["digest"])
    assert done.status_code == 200, done.text
    voice = await notes.read(db_session, bot.id, "规则/口吻")
    assert voice is not None
    assert (voice.body, voice.summary, voice.title, voice.pinned, voice.version) == (
        "第二版，没有写头。\n",
        "说话的规矩",
        "口吻",
        True,
        2,
    )
    # 同一个摘要拿去确认「不覆盖」的导入：选项变了，对不上。
    stale = await _post(client, newer, dry_run="false", overwrite="false", expect=plan["digest"])
    assert stale.status_code == 409 and code(stale) == "plan_changed"


async def test_nothing_is_written_without_a_matching_digest(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    for fields in ({"dry_run": "false"}, {"dry_run": "false", "expect": "0" * 64}):
        refused = await _post(client, SAMPLE, **fields)
        assert refused.status_code == 409 and code(refused) == "plan_changed"
    # 预览之后助理先写了其中一页：清单变了，旧摘要作废。
    digest = (await _post(client, SAMPLE)).json()["data"]["digest"]
    await notes.write(db_session, bot.id, "人/李四", body="助理先写了", summary="")
    await db_session.commit()
    refused = await _post(client, SAMPLE, dry_run="false", expect=digest)
    assert refused.status_code == 409 and code(refused) == "plan_changed"
    assert await raw(db_session, PAGES, bot=bot.id) == 1
    assert await raw(db_session, FOLLOWUPS, bot=bot.id) == 0
    assert {action for action, _ in await _logged(db_session)} == {"import.preview"}


async def test_a_failure_half_way_rolls_everything_back(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, _owner = await owned(client, db_session)
    digest = (await _post(client, SAMPLE)).json()["data"]["digest"]

    async def refuse_add(*args: object, **kwargs: object) -> None:
        raise followups.FollowupError("too_many_followups")

    # 两页都写下去之后，待跟进这一步失败。
    monkeypatch.setattr(followups, "add", refuse_add)
    failed = await _post(client, SAMPLE, dry_run="false", expect=digest)
    assert failed.status_code == 409 and code(failed) == "too_many_followups"
    assert await raw(db_session, PAGES, bot=bot.id) == 0
    assert await raw(db_session, FOLLOWUPS, bot=bot.id) == 0
    # 没有写成：不留 import.notes；清单读过库，照预览记。
    assert [action for action, _ in await _logged(db_session)] == [
        "import.preview",
        "import.preview",
    ]


async def test_bad_items_are_skipped_and_the_rest_is_imported(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    mixed = pack(
        {
            "好.md": "正文",
            "a/b/c/d/e/f/g/h/i.md": "层数太多",
            "空.md": "  \n",
            "坏头.md": "---\npinned: maybe\n---\nx",
            "好 .md": "规范化之后和第一个重名",
        }
    )
    plan = (await _post(client, mixed)).json()["data"]
    assert plan["create"] == [{"path": "好", "pinned": False}]
    assert plan["skip"] == [
        {"path": "a/b/c/d/e/f/g/h/i", "reason": "invalid_path"},
        {"path": "空", "reason": "empty_page"},
        {"path": "坏头", "reason": "invalid_front_matter"},
        {"path": "好", "reason": "duplicate_path"},
    ]
    done = await _post(client, mixed, dry_run="false", expect=plan["digest"])
    assert done.status_code == 200 and await raw(db_session, PAGES, bot=bot.id) == 1


@pytest.mark.parametrize(
    ("data", "status", "reason"),
    [
        (b"not a zip", 422, "invalid_archive"),
        (pack({"../外面.md": "x"}), 422, "unsafe_path"),
        (special("链接.md", stat.S_IFLNK), 422, "symlink_not_allowed"),
        (encrypted("密.md"), 422, "encrypted_entry"),
        (pack({"里面.zip": b"x"}), 422, "nested_archive"),
        (pack({"伪装.md": pack({"x.md": "x"})}), 422, "nested_archive"),
        (pack({"照片.png": b"x"}), 422, "unsupported_file"),
        (pack({"页.md": "中文".encode("gbk")}), 422, "not_utf8"),
        (pack({"followups.json": "[]"}), 422, "invalid_followups"),
    ],
)
async def test_an_unsafe_archive_is_refused_whole(
    client: httpx.AsyncClient, db_session: AsyncSession, data: bytes, status: int, reason: str
) -> None:
    bot, _owner = await owned(client, db_session)
    for fields in ({}, {"dry_run": "false", "expect": "0" * 64}):
        response = await _post(client, data, **fields)
        assert response.status_code == status, response.text
        assert code(response) == reason
    assert await raw(db_session, PAGES, bot=bot.id) == 0
    # 包本身被拒绝：没有读库，也就没有访问记录。
    assert await _logged(db_session) == []


@pytest.mark.parametrize(
    ("name", "value", "status", "reason"),
    [
        ("MAX_ARCHIVE_BYTES", 100, 413, "archive_too_large"),
        ("MAX_PAGE_FILES", 1, 422, "too_many_entries"),
        ("MAX_UNPACKED_BYTES", 10, 422, "unpacked_too_large"),
    ],
)
async def test_size_limits_refuse_the_archive(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: int,
    status: int,
    reason: str,
) -> None:
    await owned(client, db_session)
    monkeypatch.setattr(archive, name, value)
    response = await _post(client, SAMPLE)
    assert response.status_code == status and code(response) == reason


async def test_the_refusal_names_the_offending_file(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await owned(client, db_session)
    response = await _post(client, pack({"好.md": "x", "照片.png": b"x"}))
    assert "照片.png" in response.json()["message"]


@pytest.mark.parametrize(
    ("module", "name", "reason"),
    [(notes, "MAX_PAGES", "too_many_pages"), (followups, "MAX_OPEN", "too_many_followups")],
)
async def test_an_import_that_would_exceed_the_limits_is_refused(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    name: str,
    reason: str,
) -> None:
    bot, _owner = await owned(client, db_session)
    monkeypatch.setattr(module, name, 0)
    response = await _post(client, SAMPLE)
    assert response.status_code == 409 and code(response) == reason
    assert await raw(db_session, PAGES, bot=bot.id) == 0


@pytest.mark.parametrize("fields", [{"dry_run": "yes"}, {"overwrite": "1"}, {"expect": "x" * 65}])
async def test_form_fields_are_strict(
    client: httpx.AsyncClient, db_session: AsyncSession, fields: dict[str, str]
) -> None:
    await owned(client, db_session)
    assert (await _post(client, SAMPLE, **fields)).status_code == 422
    assert (await client.post(IMPORT, data={"dry_run": "true"})).status_code == 422


async def test_importing_needs_a_usable_assistant(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await owned(client, db_session)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    locked = await _post(client, SAMPLE)
    assert locked.status_code == 409 and code(locked) == "content_locked"
    bot.assistant_state = "deprovisioning"
    await db_session.commit()
    stopping = await _post(client, SAMPLE)
    assert stopping.status_code == 409 and code(stopping) == "assistant_not_active"


async def test_only_the_owner_in_person_can_import(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, owner = await owned(client, db_session)
    # 没有助理的平台管理员：404，碰不到所有者的知识库。
    await login_as(client, db_session, role="platform_admin")
    refused = await _post(client, SAMPLE)
    assert refused.status_code == 404 and code(refused) == "no_assistant"
    # 有自己助理的平台管理员：导进去的是他自己的知识库。
    theirs, _admin = await owned(client, db_session, role="platform_admin")
    digest = (await _post(client, SAMPLE)).json()["data"]["digest"]
    assert (await _post(client, SAMPLE, dry_run="false", expect=digest)).status_code == 200
    assert await raw(db_session, PAGES, bot=bot.id) == 0
    assert await raw(db_session, PAGES, bot=theirs.id) == 2
    # 管理命令以所有者的身份建的会话：不是本人在场。
    await as_cli(client, db_session, owner)
    refused = await _post(client, SAMPLE)
    assert refused.status_code == 403 and code(refused) == "cli_session_refused"
    # 机器人令牌：即使是所有者的也不行。
    as_bot_token(client, monkeypatch, owner)
    assert (await _post(client, SAMPLE)).status_code == 403
    assert await raw(db_session, PAGES, bot=bot.id) == 0


async def test_system_clutter_is_ignored_not_refused(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    zipped = pack(
        {
            "好.md": "正文",
            ".DS_Store": b"\x00\x01",
            "__MACOSX/._好.md": b"\x00\x05\x16\x07",
            "Thumbs.db": b"\xd0\xcf",
        }
    )
    plan = (await _post(client, zipped)).json()["data"]
    assert plan["ignored"] == 3
    assert plan["create"] == [{"path": "好", "pinned": False}] and plan["skip"] == []
    done = await _post(client, zipped, dry_run="false", expect=plan["digest"])
    assert done.status_code == 200 and await raw(db_session, PAGES, bot=bot.id) == 1
    # 别的非 Markdown 文件仍然让整个包被拒绝。
    refused = await _post(client, pack({"好.md": "正文", ".DS_Store": b"x", "照片.png": b"x"}))
    assert refused.status_code == 422 and code(refused) == "unsupported_file"


class _Endless:
    """一个读不完的上传：记下被读走了多少字节。"""

    def __init__(self) -> None:
        self.taken = 0

    async def read(self, size: int = -1) -> bytes:
        assert size > 0, "must be read in bounded chunks"
        self.taken += size
        return b"x" * size


async def test_an_oversized_upload_is_cut_off_while_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coreman.api.routers import me_assistant_transfer

    monkeypatch.setattr(archive, "MAX_ARCHIVE_BYTES", 1000)
    monkeypatch.setattr(me_assistant_transfer, "UPLOAD_CHUNK", 300)
    upload = _Endless()
    with pytest.raises(archive.ArchiveError) as refused:
        await me_assistant_transfer._receive(upload)  # type: ignore[arg-type]
    assert refused.value.code == "archive_too_large"
    # 一过上限就停：没有把整个上传读进内存再判断。
    assert upload.taken <= 1000 + 300


async def test_a_header_value_that_cannot_be_turned_into_text_is_skipped_not_a_crash(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """十六进制写的超大整数：读得出来、转不成字符串。只跳过这一个文件，不是 500。"""
    await owned(client, db_session)
    huge = "0x" + "f" * 4000
    odd = pack(
        {
            "好.md": "正文",
            "大标题.md": f"---\ntitle: {huge}\n---\nx",
            "大摘要.md": f"---\nsummary: {huge}\n---\nx",
            "大置顶.md": f"---\npinned: {huge}\n---\nx",
        }
    )
    response = await _post(client, odd)
    assert response.status_code == 200, response.text
    plan = response.json()["data"]
    assert plan["create"] == [{"path": "好", "pinned": False}]
    assert plan["skip"] == [
        {"path": "大标题", "reason": "invalid_front_matter"},
        {"path": "大摘要", "reason": "invalid_front_matter"},
        {"path": "大置顶", "reason": "invalid_front_matter"},
    ]

"""知识库的五个工具：入参校验、结果的样子、登记在 knowledge 组。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import notes, notes_tools
from coreman.core.assistants import tools as registry
from coreman.core.feishu_personal.policy import Scope
from tests.integration.approval_helpers import assistant_turn
from tests.integration.assistant_helpers import SECRET, raw, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
NAMES = {"notes_index", "notes_read", "notes_search", "notes_write", "notes_delete"}


async def call(session: AsyncSession, scope: Scope, name: str, **arguments: Any) -> dict[str, Any]:
    out = await notes_tools.dispatch(session, None, scope, name, arguments)  # type: ignore[arg-type]
    await session.commit()
    return out


def test_five_tools_are_registered_in_the_knowledge_group() -> None:
    assert {item["name"] for item in notes_tools.DEFINITIONS} == NAMES
    listed = {item["name"] for item in registry.definitions([], groups=("knowledge",))}
    assert NAMES <= listed
    assert not NAMES & {item["name"] for item in registry.definitions([], groups=("jobs",))}
    # 登记在注册表自己的文件末尾：清单不取决于别的模块有没有被导入。
    assert {registry.group_of(name) for name in NAMES} == {"knowledge"}


def test_definitions_describe_their_arguments() -> None:
    by_name = {item["name"]: item for item in notes_tools.DEFINITIONS}
    write = by_name["notes_write"]["inputSchema"]
    assert set(write["properties"]) == {"path", "body", "summary", "title", "pinned", "version"}
    assert set(write["required"]) == {"path", "body", "summary"}
    assert by_name["notes_read"]["inputSchema"]["required"] == ["path"]
    assert set(by_name["notes_index"]["inputSchema"]["properties"]) == {"folder", "limit", "offset"}
    assert "version_conflict" in by_name["notes_write"]["description"]


async def test_write_read_and_delete_round_trip(db_session: AsyncSession) -> None:
    bot, _, scope = await assistant_turn(db_session)
    saved = await call(
        db_session,
        scope,
        "notes_write",
        path="People/Li.md",
        body=f"李四负责报价。{SECRET}",
        summary="负责报价的同事",
    )
    assert saved == {"status": "saved", "path": "People/Li.md", "version": 1, "pinned": False}
    stored = await raw(
        db_session,
        "SELECT path_enc || body_enc FROM assistant_pages WHERE bot_id = :bot",
        bot=bot.id,
    )
    assert SECRET not in str(stored)
    page = await call(db_session, scope, "notes_read", path="people/li.md")
    assert page["body"] == f"李四负责报价。{SECRET}" and page["version"] == 1
    assert page["title"] == "Li" and page["truncated"] is False and page["next_offset"] is None
    assert isinstance(page["updated_at"], str)
    # 写入时记下是助理写的。
    author = "SELECT updated_by FROM assistant_pages WHERE bot_id = :bot"
    assert await raw(db_session, author, bot=bot.id) == "assistant"
    assert await call(db_session, scope, "notes_delete", path="people/li.md") == {
        "status": "deleted"
    }
    missing = {"error": "page_not_found"}
    assert await call(db_session, scope, "notes_delete", path="people/li.md") == missing
    assert await call(db_session, scope, "notes_read", path="people/li.md") == missing


async def test_write_reports_conflicts_and_limits_as_tool_errors(db_session: AsyncSession) -> None:
    _, _, scope = await assistant_turn(db_session)
    first = await call(db_session, scope, "notes_write", path="a.md", body="v1", summary="甲")
    assert first["version"] == 1
    again = await call(db_session, scope, "notes_write", path="A.md", body="v1b", summary="甲")
    assert again == {"error": "page_exists", "current_version": 1}
    second = await call(
        db_session,
        scope,
        "notes_write",
        path="a.md",
        body="v2",
        summary="甲",
        pinned=True,
        version=1,
    )
    assert second == {"status": "saved", "path": "a.md", "version": 2, "pinned": True}
    stale = await call(
        db_session, scope, "notes_write", path="a.md", body="v3", summary="甲", version=1
    )
    assert stale == {"error": "version_conflict", "current_version": 2}
    bad_path = await call(db_session, scope, "notes_write", path="a/../b", body="x", summary="")
    assert bad_path == {"error": "invalid_path"}


async def test_long_pages_are_read_in_slices(db_session: AsyncSession) -> None:
    _, _, scope = await assistant_turn(db_session)
    body = "甲" * 10 + "乙" * 10
    await call(db_session, scope, "notes_write", path="long.md", body=body, summary="长页")
    head = await call(db_session, scope, "notes_read", path="long.md", text_limit=10)
    assert head["body"] == "甲" * 10 and head["truncated"] is True and head["next_offset"] == 10
    assert head["total_chars"] == 20
    tail = await call(
        db_session, scope, "notes_read", path="long.md", text_offset=10, text_limit=10
    )
    assert tail["body"] == "乙" * 10 and tail["truncated"] is False


async def test_index_is_paged_and_search_returns_hits(db_session: AsyncSession) -> None:
    _, _, scope = await assistant_turn(db_session)
    for name in ("c", "a", "b"):
        await call(
            db_session,
            scope,
            "notes_write",
            path=f"log/{name}.md",
            body=f"{name} 提到报价",
            summary=name,
        )
    await call(db_session, scope, "notes_write", path="other/x.md", body="无关", summary="")
    page = await call(db_session, scope, "notes_index", limit=2)
    assert [item["path"] for item in page["pages"]] == ["log/a.md", "log/b.md"]
    assert page["total"] == 4 and page["next_offset"] == 2
    assert set(page["pages"][0]) == {"path", "title", "summary", "pinned", "version", "updated_at"}
    rest = await call(db_session, scope, "notes_index", limit=2, offset=2)
    assert [item["path"] for item in rest["pages"]] == ["log/c.md", "other/x.md"]
    assert rest["next_offset"] is None
    folder = await call(db_session, scope, "notes_index", folder="other")
    assert [item["path"] for item in folder["pages"]] == ["other/x.md"] and folder["total"] == 1
    found = await call(db_session, scope, "notes_search", query="报价", limit=2)
    assert [hit["path"] for hit in found["hits"]] == ["log/a.md", "log/b.md"]
    assert set(found["hits"][0]) == {
        "path",
        "title",
        "summary",
        "pinned",
        "version",
        "score",
        "snippet",
    }
    assert "提到报价" in found["hits"][0]["snippet"]


async def test_bad_arguments_never_echo_values(db_session: AsyncSession) -> None:
    _, _, scope = await assistant_turn(db_session)
    for name, arguments in (
        ("notes_read", {}),
        ("notes_read", {"path": "a.md", "extra": SECRET}),
        ("notes_write", {"path": "a.md", "body": SECRET, "summary": "s", "version": "1"}),
        ("notes_write", {"path": "a.md", "body": "", "summary": "s"}),
        ("notes_search", {"query": "   "}),
        ("notes_index", {"limit": 500}),
    ):
        out = await call(db_session, scope, name, **arguments)
        assert out["error"] == "invalid_tool_or_arguments" and out["invalid"], (name, arguments)
        assert SECRET not in str(out)
    assert await call(db_session, scope, "notes_rename", path="a.md") == {
        "error": "invalid_tool_or_arguments"
    }


async def test_only_assistants_have_these_tools(db_session: AsyncSession) -> None:
    _, _, scope = await assistant_turn(db_session)
    standard = await seed_standard(db_session)
    out = await call(db_session, replace(scope, bot=standard), "notes_index")
    assert out == {"error": "invalid_tool_or_arguments"}


async def test_another_assistants_scope_sees_nothing(db_session: AsyncSession) -> None:
    _, _, mine = await assistant_turn(db_session)
    await call(db_session, mine, "notes_write", path="a.md", body="我的页", summary="我的")
    _, _, theirs = await assistant_turn(db_session, who="lisi")
    assert (await call(db_session, theirs, "notes_index"))["pages"] == []
    assert await call(db_session, theirs, "notes_read", path="a.md") == {"error": "page_not_found"}
    assert (await call(db_session, theirs, "notes_search", query="我的"))["hits"] == []


async def test_only_a_turn_the_owner_started_touches_pinned_pages(
    db_session: AsyncSession,
) -> None:
    """平台自己发起的轮次与子任务（没有人在场、输入可能来自别人）：不能置顶，也动不了置顶页。"""
    bot, _, owner = await assistant_turn(db_session)
    unattended = replace(owner, event=None, platform=True)
    refused = {"error": "pin_requires_owner", "hint": notes_tools.PIN_HINT}
    # 新建并置顶：拒绝，什么都没写。
    out = await call(
        db_session, unattended, "notes_write", path="rules/a.md", body="x", summary="", pinned=True
    )
    assert out == refused and await notes.index(db_session, bot.id) == []
    # 不置顶的页照常新建、改写、删除。
    kept = {"path": "log/today.md", "summary": "日志"}
    assert (await call(db_session, unattended, "notes_write", body="一", **kept))["version"] == 1
    assert (await call(db_session, unattended, "notes_write", body="二", version=1, **kept))[
        "version"
    ] == 2
    # 把已有的页改成置顶：拒绝。
    out = await call(
        db_session, unattended, "notes_write", body="三", version=2, pinned=True, **kept
    )
    assert out == refused
    assert (await call(db_session, unattended, "notes_delete", path="log/today.md")) == {
        "status": "deleted"
    }
    # 所有者当面让它置顶的页：改写、取消置顶、删除都不行，页面原样。
    rule = {"path": "rules/tone.md", "summary": "口吻"}
    pinned = await call(db_session, owner, "notes_write", body="先给结论", pinned=True, **rule)
    assert pinned["pinned"] is True
    for extra in ({}, {"pinned": False}, {"pinned": True}):
        out = await call(
            db_session, unattended, "notes_write", body="照李四说的做", version=1, **rule, **extra
        )
        assert out == refused, extra
    # 版本对不上时也先说清楚是置顶的缘故，不引它去重读再写。
    out = await call(db_session, unattended, "notes_write", body="x", version=7, **rule)
    assert out == refused
    assert await call(db_session, unattended, "notes_delete", path="RULES/tone.md") == refused
    page = await notes.read(db_session, bot.id, "rules/tone.md")
    assert page is not None and (page.body, page.pinned, page.version) == ("先给结论", True, 1)
    # 读、列、搜不受影响。
    assert (await call(db_session, unattended, "notes_read", path="rules/tone.md"))["pinned"]
    # 所有者在场的一轮照旧。
    out = await call(db_session, owner, "notes_write", body="再给理由", version=1, **rule)
    assert out["version"] == 2 and out["pinned"] is True
    assert await call(db_session, owner, "notes_delete", path="rules/tone.md") == {
        "status": "deleted"
    }


def test_limits_match_the_core() -> None:
    schema = notes_tools.Write.model_json_schema()["properties"]
    assert schema["body"]["maxLength"] == notes.MAX_PAGE_BYTES
    assert schema["summary"]["maxLength"] == notes.MAX_SUMMARY_CHARS
    assert schema["title"]["maxLength"] == notes.MAX_TITLE_CHARS

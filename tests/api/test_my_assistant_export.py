"""导出接口：知识页与未结的待跟进打成与导入相同的格式；导出再导入，内容原样回来。"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import archive, followups, keys, notes
from coreman.core.db.models import Bot
from tests.api.conftest import login_as
from tests.api.my_assistant_helpers import URL, as_bot_token, as_cli, code, owned
from tests.integration.assistant_helpers import SECRET

pytestmark = pytest.mark.usefixtures("assistant_master")
EXPORT, IMPORT = f"{URL}/export", f"{URL}/import"


async def _fill(db_session: AsyncSession, bot: Bot) -> None:
    await notes.write(
        db_session,
        bot.id,
        "规则/口吻",
        body=f"回复要短。\n先说结论。{SECRET}\n",
        summary="说话的规矩",
        title="口吻: 要点",
        pinned=True,
    )
    # 正文以分隔线开头、没有摘要、标题取默认值的一页。
    await notes.write(db_session, bot.id, "人/李四", body="---\n正文以分隔线开头\n", summary="")
    await followups.add(
        db_session,
        bot.id,
        category="waiting",
        title="等报价",
        notes="催过一次\n第二行",
        due_on=date(2026, 12, 1),
    )
    later = await followups.add(db_session, bot.id, category="remind", title="体检")
    await followups.snooze(db_session, bot.id, later.seq, date(2099, 1, 1), today=date(2026, 1, 1))
    done = await followups.add(db_session, bot.id, category="decide", title="已经办完的")
    await followups.close(db_session, bot.id, done.seq)
    await db_session.commit()


async def _snapshot(db_session: AsyncSession, bot_id: uuid.UUID) -> tuple[list[Any], list[Any]]:
    pages = [
        (page.path, page.title, page.summary, page.pinned, page.body)
        for page in await notes.dump(db_session, bot_id)
    ]
    rows = await followups.list_(
        db_session, bot_id, "open", today=date(2026, 1, 1), limit=followups.MAX_OPEN
    )
    listed = [
        (row.category, row.title_enc, row.notes_enc, row.review_on, row.due_on, row.status)
        for row in rows
    ]
    # 提交而不是回滚：回滚会让会话里的对象过期，调用方手里的记录就不能再读了。
    await db_session.commit()
    return pages, listed


async def _logged(db_session: AsyncSession) -> list[tuple[str, str]]:
    rows = await db_session.execute(
        text("SELECT action, target FROM assistant_access_log ORDER BY id")
    )
    return [(row[0], row[1]) for row in rows.all()]


async def test_the_owner_downloads_pages_and_open_followups(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    await _fill(db_session, bot)
    response = await client.get(EXPORT)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="assistant-export-')
    assert disposition.endswith('.zip"')
    parsed = archive.parse(response.content)
    assert [(page.path, page.title, page.summary, page.pinned) for page in parsed.pages] == [
        ("人/李四", "李四", "", False),
        ("规则/口吻", "口吻: 要点", "说话的规矩", True),
    ]
    assert parsed.pages[0].body == "---\n正文以分隔线开头\n"
    assert parsed.pages[1].body == f"回复要短。\n先说结论。{SECRET}\n"
    # 已关闭的待跟进不导出；推后的照样导出。
    assert [(item.category, item.title, item.snoozed) for item in parsed.followups] == [
        ("waiting", "等报价", False),
        ("remind", "体检", True),
    ]
    # 访问记录的对象只有两个数。
    assert await _logged(db_session) == [("export", "pages=2,followups=2")]


async def test_an_export_imports_back_unchanged(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    await _fill(db_session, bot)
    exported = (await client.get(EXPORT)).content
    # 另一位所有者把这个包导进自己的（空的）助理。
    theirs, _other = await owned(client, db_session)
    upload = {"archive": ("export.zip", exported, "application/zip")}
    plan = (await client.post(IMPORT, files=upload)).json()["data"]
    assert len(plan["create"]) == 2 and plan["skip"] == []
    assert plan["followups"] == {"create": 2, "skip": []}
    done = await client.post(
        IMPORT, files=upload, data={"dry_run": "false", "expect": plan["digest"]}
    )
    assert done.status_code == 200, done.text
    assert await _snapshot(db_session, theirs.id) == await _snapshot(db_session, bot.id)


async def test_an_empty_assistant_exports_an_empty_archive(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await owned(client, db_session)
    response = await client.get(EXPORT)
    assert response.status_code == 200
    parsed = archive.parse(response.content)
    assert parsed.pages == [] and parsed.followups == []
    assert await _logged(db_session) == [("export", "pages=0,followups=0")]


async def test_a_destroyed_key_exports_nothing(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await owned(client, db_session)
    await _fill(db_session, bot)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    response = await client.get(EXPORT)
    assert response.status_code == 409 and code(response) == "content_locked"
    assert SECRET not in response.text and await _logged(db_session) == []


async def test_only_the_owner_in_person_can_export(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, owner = await owned(client, db_session)
    await _fill(db_session, bot)
    # 没有助理的平台管理员。
    await login_as(client, db_session, role="platform_admin")
    refused = await client.get(EXPORT)
    assert refused.status_code == 404 and code(refused) == "no_assistant"
    # 有自己助理的平台管理员：拿到的是他自己的（空的）。
    await owned(client, db_session, role="platform_admin")
    theirs = await client.get(EXPORT)
    assert archive.parse(theirs.content).pages == []
    assert SECRET.encode() not in theirs.content
    # 管理命令以所有者的身份建的会话：不是本人在场。
    await as_cli(client, db_session, owner)
    refused = await client.get(EXPORT)
    assert refused.status_code == 403 and code(refused) == "cli_session_refused"
    assert SECRET.encode() not in refused.content
    # 机器人令牌：即使是所有者的也不行。
    as_bot_token(client, monkeypatch, owner)
    assert (await client.get(EXPORT)).status_code == 403
    # 一路下来，所有者的助理没有被导出过。
    assert ("export", "pages=2,followups=2") not in await _logged(db_session)

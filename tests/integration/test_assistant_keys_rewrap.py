"""主密钥轮换：数据密钥重新包裹后不再依赖旧主密钥，内容不重写。"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman import cli
from coreman.core.assistants import keys
from coreman.core.config import reset_settings_cache
from coreman.core.crypto import Cipher, key_id_of
from coreman.core.db.models import AssistantKey, User
from tests.integration.assistant_helpers import seed_assistant

OLD = b"\x05" * 32
NEW = b"\x06" * 32


@pytest.fixture(autouse=True)
def _clean_cache() -> Iterator[None]:
    keys.forget()
    yield
    keys.forget()


async def test_rewrap_moves_keys_to_the_current_master(db_session: AsyncSession) -> None:
    owners = [User(login_name=f"u{i}", display_name="张三") for i in range(3)]
    db_session.add_all(owners)
    await db_session.flush()
    old = Cipher(OLD)
    for owner in owners:
        await keys.ensure_key(db_session, old, owner.id)
    await keys.destroy_key(db_session, owners[2].id)
    await db_session.commit()
    token = (await keys.data_cipher(db_session, old, owners[0].id)).encrypt("机密", "t.c:x")
    rotated = Cipher(NEW, key_id="k2", previous={"k1": OLD})
    touched: list[uuid.UUID] = []
    assert await keys.rewrap(db_session, rotated, touched=touched) == (0, 2)
    # 只报真的重新包裹了的那两把；已销毁的不算。
    assert set(touched) == {owners[0].id, owners[1].id}
    await db_session.commit()
    rows = (await db_session.execute(select(AssistantKey))).scalars().all()
    assert {key_id_of(r.dek_enc) for r in rows if r.dek_enc} == {"k2"}
    # 重新包裹只换外层：代数不变，内容密文里的 d1 仍然对得上。
    assert {r.generation for r in rows} == {1}
    # 再跑一次什么都不用做。
    assert await keys.rewrap(db_session, rotated) == (2, 0)
    # 旧主密钥移除之后仍读得出，内容密文没有动。
    only_new = Cipher(NEW, key_id="k2")
    assert (await keys.data_cipher(db_session, only_new, owners[0].id)).decrypt(
        token, "t.c:x"
    ) == "机密"


@pytest.mark.usefixtures("runtime_settings")
async def test_command_only_counts_unless_applied(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bot, owner = await seed_assistant(db_session)
    # 下面的 stored_kid 会让会话里的对象过期：之后还要用的编号先取出来。
    bot_id = bot.id
    await keys.ensure_key(db_session, Cipher(OLD, key_id="k0"), owner.id)

    async def logged() -> list[tuple[Any, ...]]:
        rows = await db_session.execute(
            text(
                "SELECT bot_id, actor_id, actor_kind, action, target"
                " FROM assistant_access_log ORDER BY id"
            )
        )
        return [tuple(row) for row in rows.all()]

    await db_session.commit()
    monkeypatch.setenv("MASTER_KEYS_PREVIOUS", "k0:" + base64.b64encode(OLD).decode())
    reset_settings_cache()

    async def stored_kid() -> str | None:
        db_session.expire_all()
        row = (await db_session.execute(select(AssistantKey))).scalar_one()
        return key_id_of(row.dek_enc or "")

    assert await cli._assistant_keys_rewrap(False) == 0
    assert json.loads(capsys.readouterr().out) == {"current": 0, "stale": 1}
    assert await stored_kid() == "k0"
    # 只统计的那一次不落库，也就不留访问记录。
    assert await logged() == []
    assert await cli._assistant_keys_rewrap(True) == 0
    assert json.loads(capsys.readouterr().out) == {"current": 0, "rewrapped": 1}
    assert await stored_kid() == "k1"
    # 不看内容的密钥操作，也让所有者在访问记录里看得到。
    assert await logged() == [(bot_id, None, "system", "cli.rewrap", "")]

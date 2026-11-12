"""assistant_approvals / assistant_marks：约束，以及三列内容在库里是密文。"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.db.models import AssistantApproval, AssistantMark
from coreman.core.db.session import make_session_factory
from coreman.core.timeutils import utcnow
from tests.conftest import BUSINESS_TABLES
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
STORED = "SELECT args_enc::text || coalesce(reason_enc, '') FROM assistant_approvals WHERE id = :id"


def _row(bot_id: uuid.UUID, **kw: object) -> AssistantApproval:
    values: dict[str, object] = {
        "bot_id": bot_id,
        "tool": "feishu_reply_message",
        "args_enc": {"calls": [{"arguments": {"text": SECRET}}], "abstract": "私聊 → 李四"},
        "args_hash": "h1",
        "request_hash": "r1",
        "channel": "p2p",
        "external_id": f"cm-pa-{uuid.uuid4().hex}",
        "expires_at": utcnow() + timedelta(hours=72),
    }
    return AssistantApproval(**{**values, **kw})


def test_tables_are_truncated_before_bots() -> None:
    for name in ("assistant_approvals", "assistant_marks"):
        assert BUSINESS_TABLES.index(name) < BUSINESS_TABLES.index("bots")


async def test_content_is_sealed_and_reads_back(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    row = _row(bot.id, reason_enc=f"理由-{SECRET}")
    db_session.add(row)
    await db_session.commit()
    assert row.status == "pending" and row.args_enc is not None and row.has_files is False
    stored = str(await raw(db_session, STORED, id=row.id))
    assert SECRET not in stored and "李四" not in stored and stored.count("enc:v2:d1:") == 2
    async with make_session_factory(db_engine)() as other:
        found = await other.get(AssistantApproval, row.id)
        assert found is not None and found.args_enc == row.args_enc
        assert found.reason_enc == f"理由-{SECRET}"
        # 密钥销毁之后：行还在、元数据可读，内容为空且不抛错。
        await keys.destroy_key(other, owner.id)
        await other.commit()
        locked = await other.get(AssistantApproval, row.id, populate_existing=True)
        assert locked is not None and sealed.is_locked(locked)
        assert locked.args_enc is None and locked.reason_enc is None
        assert locked.tool == "feishu_reply_message" and locked.channel == "p2p"


async def test_status_channel_and_external_id_are_constrained(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    bot_id = bot.id
    for bad in ({"status": "done"}, {"channel": "sms"}):
        db_session.add(_row(bot_id, **bad))
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()
    db_session.add_all([_row(bot_id, external_id="cm-pa-x"), _row(bot_id, external_id="cm-pa-x")])
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_one_live_approval_per_content(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    db_session.add(_row(bot.id, status="executed"))
    db_session.add(_row(bot.id, status="rejected"))
    db_session.add(_row(bot.id))
    await db_session.flush()
    # 结束了的单不占位；在途的同一份内容只能有一张。
    db_session.add(_row(bot.id, status="approved"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_marks_are_one_per_message_and_go_with_the_bot(db_session: AsyncSession) -> None:
    bot = await seed_standard(db_session)
    db_session.add(AssistantMark(bot_id=bot.id, message_id="om_1", chat_id="oc_1", reaction_id="r"))
    await db_session.flush()
    db_session.add(AssistantMark(bot_id=bot.id, message_id="om_1", chat_id="oc_1", reaction_id="r"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_content_mac_is_keyed_per_assistant(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    other, _ = await seed_assistant(db_session)
    first = await sealed.content_mac(db_session, bot.id, "p", "同一段内容")
    assert first == await sealed.content_mac(db_session, bot.id, "p", "同一段内容")
    assert first != await sealed.content_mac(db_session, other.id, "p", "同一段内容")
    assert first != await sealed.content_mac(db_session, bot.id, "p", "另一段内容")
    standard = await seed_standard(db_session)
    with pytest.raises(ValueError):
        await sealed.content_mac(db_session, standard.id, "p", "x")
    await keys.destroy_key(db_session, owner.id)
    with pytest.raises(sealed.ContentLocked):
        await sealed.content_mac(db_session, bot.id, "p", "同一段内容")

"""跨机器人的批处理里，一行助理内容解不开（密文被改、主密钥配错）只影响这一行。

认领与看护一次处理很多机器人的行：一行抛 `DecryptError` 会让整批回滚，下一轮又撞上同一行，
普通机器人跟着停摆。这些地方把这一行当作读不出来处理；单个机器人自己的读取照旧报错。
"""

from __future__ import annotations

import base64
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.bus import instances, streams, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.crypto import Cipher, DecryptError
from coreman.core.db.session import make_session_factory
from coreman.core.i18n.messages import msg
from coreman.runtime.scheduler import reaper
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
WORKER = "worker-a:h:1:1"


def _chat(bot_id) -> NewTask:  # type: ignore[no-untyped-def]
    payload = {"message": {"text": SECRET, "chat_id": "oc_1"}, "platform_user_id": "ou_1"}
    return NewTask(bot_id=bot_id, kind="chat", payload=payload, session_key="oc_1")


def _corrupt(token: str) -> str:
    """同一代密钥、格式合法，但认证标签对不上：解密时抛 DecryptError，而不是按已锁定处理。"""
    prefix, _, body = token.rpartition(":")
    data = bytearray(base64.b64decode(body))
    data[-1] ^= 0x01
    return f"{prefix}:{base64.b64encode(bytes(data)).decode()}"


async def _poison_payload(session: AsyncSession, table: str, row_id: int) -> None:
    """把一行载荷封装里的密文改坏（tasks、outbox 的 payload 列）。"""
    sql = f"SELECT payload->>'_sealed' FROM {table} WHERE id = :id"
    token = await raw(session, sql, id=row_id)
    await session.execute(
        text(
            f"UPDATE {table} SET payload = "
            "jsonb_set(payload, '{_sealed}', to_jsonb(CAST(:t AS text))) WHERE id = :id"
        ),
        {"t": _corrupt(token), "id": row_id},
    )


async def _poison_task(session: AsyncSession, task_id: int) -> None:
    await _poison_payload(session, "tasks", task_id)


async def _poison_stream(session: AsyncSession, task_id: int) -> None:
    token = await raw(
        session, "SELECT pending_text FROM task_streams WHERE task_id = :id", id=task_id
    )
    await session.execute(
        text("UPDATE task_streams SET pending_text = :t WHERE task_id = :id"),
        {"t": _corrupt(token), "id": task_id},
    )


async def _column(session: AsyncSession, column: str, task_id: int) -> str:
    return str(await raw(session, f"SELECT {column}::text FROM tasks WHERE id = :id", id=task_id))


async def _stream(session: AsyncSession, bot_id, task_id: int) -> None:  # type: ignore[no-untyped-def]
    await streams.create(
        session,
        task_id=task_id,
        bot_id=bot_id,
        platform="feishu",
        stream_id=f"s{task_id}",
        reply_context={"chat_id": "oc_1"},
        lease_generation=1,
        running_since=datetime.now(UTC),
    )
    await streams.update(session, task_id, pending_text=f"做到一半-{task_id}", bot_id=bot_id)


async def test_claim_fails_the_undecryptable_task_and_moves_on(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    assistant, _ = await seed_assistant(db_session)
    standard = await seed_standard(db_session)
    await instances.register(
        db_session, instance_id=WORKER, service="worker", version="dev", capacity=30
    )
    poisoned = await tasks.enqueue(db_session, _chat(assistant.id))
    healthy = await tasks.enqueue(db_session, _chat(standard.id))
    assert poisoned is not None and healthy is not None and poisoned.id < healthy.id
    await _poison_task(db_session, poisoned.id)
    await db_session.commit()
    factory = make_session_factory(db_engine)
    # 单个机器人自己的读取照旧报错：主密钥配错要响亮。
    async with factory() as own:
        with pytest.raises(DecryptError):
            await tasks.get(own, poisoned.id)
    async with factory() as session:
        assert await tasks.claim(session, lane="normal", instance_id=WORKER) is None
        await session.commit()
    assert await _column(db_session, "status", poisoned.id) == "failed"
    assert await _column(db_session, "error_code", poisoned.id) == "content_undecryptable"
    async with factory() as session:
        claimed = await tasks.claim(session, lane="normal", instance_id=WORKER)
        await session.commit()
    assert claimed is not None and claimed.id == healthy.id
    assert claimed.payload["message"]["text"] == SECRET


async def _running_pair(db_session: AsyncSession) -> tuple[int, int]:
    """一个助理、一个普通机器人各一条失联的任务（各带一条半截的流），助理那条解不开。"""
    assistant, _ = await seed_assistant(db_session)
    standard = await seed_standard(db_session)
    ids = []
    for bot in (assistant, standard):
        task = await tasks.enqueue(db_session, _chat(bot.id))
        assert task is not None
        await _stream(db_session, bot.id, task.id)
        ids.append(task.id)
    await db_session.execute(
        text(
            "UPDATE tasks SET status = 'running', claimed_by = NULL, "
            "claimed_at = now() - interval '100 seconds', "
            "heartbeat_at = now() - interval '100 seconds' WHERE id = ANY(:ids)"
        ),
        {"ids": ids},
    )
    await _poison_task(db_session, ids[0])
    await _poison_stream(db_session, ids[0])
    await db_session.commit()
    return ids[0], ids[1]


async def test_reap_lost_tasks_reaps_the_whole_batch(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    poisoned, healthy = await _running_pair(db_session)
    async with make_session_factory(db_engine)() as session:
        assert await reaper.reap_lost_tasks(session, datetime.now(UTC)) == 2
        await session.commit()
    for task_id in (poisoned, healthy):
        assert await _column(db_session, "status", task_id) == "failed"
        assert await _column(db_session, "error_code", task_id) == "worker_lost"
    async with make_session_factory(db_engine)() as session:
        stream = await streams.get(session, healthy)
        assert stream is not None and stream.is_complete
        assert stream.final_text == f"做到一半-{healthy}\n\n" + msg("worker_lost")
        # 解不开的那条流照样结掉，只是没有中断前的正文。
        assert await raw(
            session, "SELECT is_complete FROM task_streams WHERE task_id = :id", id=poisoned
        )


async def test_reap_lost_tasks_survives_a_wrong_master_key(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """主密钥配错：读不出、也写不进助理的内容，这一行照样收尸，普通机器人的不受牵连。"""
    poisoned, healthy = await _running_pair(db_session)
    sealed.use_master(Cipher(os.urandom(32)))
    keys.forget()
    sealed.forget()
    async with make_session_factory(db_engine)() as session:
        assert await reaper.reap_lost_tasks(session, datetime.now(UTC)) == 2
        await session.commit()
    for task_id in (poisoned, healthy):
        assert await _column(db_session, "status", task_id) == "failed"
    assert await raw(
        db_session, "SELECT is_complete FROM task_streams WHERE task_id = :id", id=healthy
    )


async def test_recover_terminal_streams_recovers_the_whole_batch(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    poisoned, healthy = await _running_pair(db_session)
    for task_id in (poisoned, healthy):
        await tasks.finish(db_session, task_id, status="failed", error_code="x")
    await db_session.commit()
    later = datetime.now(UTC) + timedelta(seconds=90)
    async with make_session_factory(db_engine)() as session:
        assert await reaper.recover_terminal_streams(session, later) == 2
        await session.commit()
    async with make_session_factory(db_engine)() as session:
        stream = await streams.get(session, healthy)
        assert stream is not None and stream.is_complete
        assert stream.final_text.startswith(f"做到一半-{healthy}")
        assert await raw(
            session, "SELECT is_complete FROM task_streams WHERE task_id = :id", id=poisoned
        )


async def test_docs_resume_tick_expires_the_undecryptable_wait(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """等授权续接的看护：来源任务解不开就不续接；同一批里普通机器人的行照常处理。"""
    from coreman.core.db.models import FeishuAuthWait
    from coreman.core.feishu_docs import resume

    assistant, owner = await seed_assistant(db_session)
    standard = await seed_standard(db_session)
    origin = await tasks.enqueue(db_session, _chat(assistant.id))
    assert origin is not None
    await _poison_task(db_session, origin.id)
    now = datetime.now(UTC)
    poisoned = FeishuAuthWait(
        bot_id=assistant.id,
        user_id=owner.id,
        chat_id="oc_1",
        origin_task_id=origin.id,
        status="waiting",
        expires_at=now + timedelta(minutes=5),
    )
    lapsed = FeishuAuthWait(
        bot_id=standard.id,
        user_id=standard.created_by,
        chat_id="oc_2",
        status="waiting",
        expires_at=now - timedelta(minutes=1),
    )
    db_session.add_all([poisoned, lapsed])
    await db_session.commit()
    async with make_session_factory(db_engine)() as session:
        assert await resume.tick(session, now, Cipher(os.urandom(32))) == 0
        await session.commit()
    for row in (poisoned, lapsed):
        await db_session.refresh(row)
        assert row.status == "expired"


async def test_credential_requests_expire_past_an_undecryptable_card(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    from coreman.core.bus import outbox
    from coreman.core.db.models import CredentialRequest
    from coreman.core.personal_credentials import service as credential_requests

    assistant, owner = await seed_assistant(db_session)
    standard = await seed_standard(db_session)
    card = await outbox.add(
        db_session,
        bot_id=assistant.id,
        platform="feishu",
        kind="send",
        dedupe_key="credential-card-1",
        target={"chat_id": "oc_1"},
        payload={"card": {"text": SECRET}, "_feishu_message_id": "om_1"},
    )
    assert card is not None
    await _poison_payload(db_session, "outbox", card.id)
    past = datetime.now(UTC) - timedelta(minutes=1)
    rows = [
        CredentialRequest(
            bot_id=bot.id,
            user_id=user_id,
            origin_kind="chat",
            origin_chat_id="oc_1",
            origin_chat_type="single",
            expires_at=past,
            request_outbox_id=outbox_id,
        )
        for bot, user_id, outbox_id in (
            (assistant, owner.id, card.id),
            (standard, standard.created_by, None),
        )
    ]
    db_session.add_all(rows)
    await db_session.commit()
    async with make_session_factory(db_engine)() as session:
        assert await credential_requests.expire_due(session, datetime.now(UTC)) == 2
        await session.commit()
    for row in rows:
        await db_session.refresh(row)
        assert row.status == "expired"

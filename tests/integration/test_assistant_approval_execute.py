"""批准之后：复查会话、核销令牌、按暂存参数原样执行、上报并投回结果；各失败分支都不执行。"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import approvals, keys
from coreman.core.assistants.approvals import execute, tower
from coreman.core.assistants.kinds import APPROVAL_EXECUTE_KIND, APPROVAL_RESULT_KIND
from coreman.core.bus import tasks
from coreman.core.crypto import Cipher
from coreman.core.db.models import AssistantApproval, Task
from coreman.core.db.session import make_session_factory
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.assistant_approvals import AssistantApprovalExecuteHandler
from tests.integration.approval_helpers import (
    CFG,
    OWNER,
    TEXT,
    FakeBucket,
    FakeFeishu,
    FakeRelay,
    FakeTower,
    assistant_turn,
    decided,
    install,
    install_files,
    message,
    pending,
    queued,
    reply,
)
from tests.integration.assistant_helpers import raw
from tests.integration.test_cron_handler import claim
from tests.integration.worker_helpers import MASTER, build_ctx

pytestmark = pytest.mark.usefixtures("assistant_master")
RESULT = "SELECT coalesce(result_enc::text, '') FROM assistant_approvals WHERE id = :id"
REPLY_PATH = "/im/v1/messages/om_2/reply"


async def approved(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any] | None = None
) -> tuple[AssistantApproval, Task, FakeFeishu, FakeTower]:
    """一张已批准的单，以及回调排下的执行任务。"""
    feishu, fake = install(monkeypatch)
    _, _, scope = await assistant_turn(session)
    row = await pending(session, scope, arguments)
    await decided(session, row)
    task = (await queued(session, APPROVAL_EXECUTE_KIND))[0]
    return row, task, feishu, fake


async def run(engine: AsyncEngine, row: AssistantApproval, task: Task) -> str:
    return await execute.run(make_session_factory(engine), Cipher(MASTER), row.id, task, CFG)


async def reload(session: AsyncSession, row: AssistantApproval) -> AssistantApproval:
    found = await session.get(AssistantApproval, row.id, populate_existing=True)
    assert found is not None
    return found


async def test_approved_write_runs_exactly_as_staged(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, task, feishu, fake = await approved(db_session, monkeypatch)
    assert await run(db_engine, row, task) == "executed"
    # 发出去的就是暂存的那份：正文、格式、重试编号一字不差。
    assert feishu.writes == [
        (
            "POST",
            REPLY_PATH,
            {
                "msg_type": "text",
                "content": json.dumps({"text": TEXT}, ensure_ascii=False),
                "reply_in_thread": False,
                "uuid": "u1",
            },
        )
    ]
    row = await reload(db_session, row)
    assert row.status == "executed" and row.executed_at is not None and row.error_code is None
    assert row.result_enc == {"results": [{"message_id": "om_sent_1", "reaction_id": "r_1"}]}
    assert "om_sent_1" not in str(await raw(db_session, RESULT, id=row.id))
    # 执行前核销了令牌，哈希就是提交时那一个；执行后上报。
    assert [(token, digest) for token, digest, _ in fake.redeemed] == [
        ("twa_" + str(row.approval_id), row.request_hash)
    ]
    assert fake.reported == [(row.approval_id, "executed", None)]
    turns = await queued(db_session, APPROVAL_RESULT_KIND)
    assert [t.payload["status"] for t in turns] == ["executed"]
    # 再跑一次（任务重投）：不是 approved 了，什么都不做。
    assert await run(db_engine, row, task) == "executed"
    assert len(feishu.writes) == 1


@pytest.mark.parametrize(
    ("newer", "code"),
    [
        (message("om_3", OWNER, "我自己回了"), "owner_replied_after_anchor"),
        (message("om_3", text="算了不用了"), "newer_messages_after_anchor"),
    ],
)
async def test_chat_moved_on_after_approval_is_stale_and_not_executed(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    newer: dict[str, Any],
    code: str,
) -> None:
    row, task, feishu, fake = await approved(db_session, monkeypatch)
    feishu.history.insert(0, newer)
    assert await run(db_engine, row, task) == "stale"
    row = await reload(db_session, row)
    assert (row.status, row.error_code) == ("stale", code)
    # 没有执行，令牌也没有取：这次批准作废，交回助理重新起草。
    assert feishu.writes == [] and fake.redeemed == []
    assert fake.reported == [(row.approval_id, "failed", "stale")]
    assert [t.payload["status"] for t in await queued(db_session, APPROVAL_RESULT_KIND)] == [
        "stale"
    ]


async def two_drafts(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> tuple[AssistantApproval, AssistantApproval, FakeFeishu]:
    """同一个会话里两张待批的回复（都回复李四的 om_2），第一张已批准并发出。"""
    feishu, _ = install(monkeypatch)
    _, _, scope = await assistant_turn(session)
    first = await pending(session, scope)
    second = await pending(
        session, scope, {**reply(), "text": "报价单我周四一并发你。", "uuid": "u2"}
    )
    await decided(session, first)
    return first, second, feishu


async def test_a_second_draft_in_the_same_chat_survives_the_first_being_sent(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """平台代发的那条在飞书里发送人是所有者，但不是他亲自回复：不让同会话的另一张单过时。"""
    first, second, feishu = await two_drafts(db_session, monkeypatch)
    task = (await queued(db_session, APPROVAL_EXECUTE_KIND))[0]
    assert await run(db_engine, first, task) == "executed"
    # 飞书里这个会话多了一条所有者名下的消息：就是刚才代发的那条。
    feishu.history.insert(0, message("om_sent_1", OWNER, TEXT))
    await decided(db_session, second)
    task = (await queued(db_session, APPROVAL_EXECUTE_KIND))[1]
    assert await run(db_engine, second, task) == "executed"
    second = await reload(db_session, second)
    assert (second.status, second.error_code) == ("executed", None)
    assert len(feishu.writes) == 2


async def test_the_owner_really_replying_still_makes_the_second_draft_stale(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second, feishu = await two_drafts(db_session, monkeypatch)
    task = (await queued(db_session, APPROVAL_EXECUTE_KIND))[0]
    assert await run(db_engine, first, task) == "executed"
    feishu.history.insert(0, message("om_sent_1", OWNER, TEXT))
    # 所有者自己又回了一句：这一条没登记在平台代发的名单里。
    feishu.history.insert(0, message("om_own", OWNER, "我再补一句"))
    await decided(db_session, second)
    task = (await queued(db_session, APPROVAL_EXECUTE_KIND))[1]
    assert await run(db_engine, second, task) == "stale"
    second = await reload(db_session, second)
    assert (second.status, second.error_code) == ("stale", "owner_replied_after_anchor")
    assert len(feishu.writes) == 1


async def test_feishu_failure_is_recorded_and_not_retried(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, task, feishu, fake = await approved(db_session, monkeypatch)
    feishu.fail = "feishu_request_failed"
    assert await run(db_engine, row, task) == "failed"
    row = await reload(db_session, row)
    assert (row.status, row.error_code) == ("failed", "feishu_request_failed")
    assert row.result_enc == {
        "results": {
            "done": [],
            "failure": {"error": "feishu_request_failed", "upstream_code": 230001},
        }
    }
    assert fake.reported == [(row.approval_id, "failed", "feishu_request_failed")]
    assert [t.payload["status"] for t in await queued(db_session, APPROVAL_RESULT_KIND)] == [
        "failed"
    ]
    feishu.fail = None
    assert await run(db_engine, row, task) == "failed" and feishu.writes == []


async def test_changed_arguments_cannot_use_the_approval(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, task, feishu, _ = await approved(db_session, monkeypatch)
    # 批准之后库里的参数被换掉：核销时哈希对不上，不执行。
    assert row.args_enc is not None
    changed = json.loads(json.dumps(row.args_enc))
    changed["calls"][0]["arguments"]["text"] = "另一段话"
    row.args_enc = changed
    await db_session.commit()
    assert await run(db_engine, row, task) == "failed"
    row = await reload(db_session, row)
    assert row.error_code == "request_mismatch" and feishu.writes == []


async def test_refused_token_fails_closed(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, task, feishu, fake = await approved(db_session, monkeypatch)
    fake.refuse = "token_used"
    assert await run(db_engine, row, task) == "failed"
    assert (await reload(db_session, row)).error_code == "token_used" and feishu.writes == []


async def test_tower_outage_changes_nothing_and_can_be_retried(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, task, feishu, fake = await approved(db_session, monkeypatch)
    fake.down = True
    with pytest.raises(execute.Retry):
        await run(db_engine, row, task)
    row = await reload(db_session, row)
    assert row.status == "approved" and row.execute_started_at is None and feishu.writes == []
    fake.down = False
    assert await run(db_engine, row, task) == "executed" and len(feishu.writes) == 1


async def test_giving_up_tells_the_assistant(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, _, feishu, fake = await approved(db_session, monkeypatch)
    assert await execute.give_up(make_session_factory(db_engine), row.id, CFG) == "failed"
    row = await reload(db_session, row)
    assert (row.status, row.error_code) == ("failed", "tower_unavailable")
    assert fake.reported == [(row.approval_id, "failed", "tower_unavailable")]
    assert feishu.writes == [] and len(await queued(db_session, APPROVAL_RESULT_KIND)) == 1


async def test_interrupted_execution_is_never_repeated(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, task, feishu, fake = await approved(db_session, monkeypatch)
    # 上一次已经开始调飞书，进程没等到结果就没了。
    row.execute_started_at = utcnow()
    await db_session.commit()
    assert await run(db_engine, row, task) == "failed"
    row = await reload(db_session, row)
    assert row.error_code == "execution_interrupted"
    assert feishu.writes == [] and fake.redeemed == []


async def test_destroyed_key_cannot_execute_and_does_not_raise(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    feishu, fake = install(monkeypatch)
    _, owner, scope = await assistant_turn(db_session)
    row = await pending(db_session, scope)
    await decided(db_session, row)
    task = (await queued(db_session, APPROVAL_EXECUTE_KIND))[0]
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    assert await run(db_engine, row, task) == "failed"
    row = await reload(db_session, row)
    assert (row.status, row.error_code) == ("failed", "content_locked")
    assert feishu.writes == [] and fake.redeemed == []
    assert await queued(db_session, APPROVAL_RESULT_KIND) == []


async def test_merged_sends_run_one_by_one(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    feishu, _ = install(monkeypatch)
    _, _, scope = await assistant_turn(db_session)
    arguments = {
        "receive_id": "oc_li",
        "receive_id_type": "chat_id",
        "text": "明早九点开会",
        "uuid": "a",
        "approval": {
            "chat_id": "oc_li",
            "anchor_message_id": "om_2",
            "more_recipients": [
                {"receive_id": "oc_wang", "anchor_message_id": "om_2", "uuid": "b"}
            ],
        },
    }
    out = await approvals.submit(
        db_session,
        None,
        scope,
        "feishu_send_message",
        arguments,
        cfg=CFG,  # type: ignore[arg-type]
    )
    await db_session.commit()
    row = await db_session.get(AssistantApproval, out["approval_id"])
    assert row is not None
    await decided(db_session, row)
    task = (await queued(db_session, APPROVAL_EXECUTE_KIND))[0]
    assert await run(db_engine, row, task) == "executed"
    assert [(w[2]["receive_id"], w[2]["uuid"]) for w in feishu.writes] == [
        ("oc_li", "a"),
        ("oc_wang", "b"),
    ]


@pytest.mark.usefixtures("runtime_settings")
async def test_worker_task_executes_and_retries_on_outage(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, _, feishu, fake = await approved(db_session, monkeypatch)
    monkeypatch.setattr(tower, "config", lambda _settings: CFG)
    fake.down = True
    claimed = await claim(db_session)
    assert claimed.kind == APPROVAL_EXECUTE_KIND
    await AssistantApprovalExecuteHandler().run(build_ctx(db_engine, claimed))
    # 审批系统连不上：任务放回队列稍后再试，审批单原样不动。
    again = await db_session.get(Task, claimed.id, populate_existing=True)
    assert again is not None and again.status == "queued"
    assert (await reload(db_session, row)).status == "approved" and feishu.writes == []
    fake.down = False
    again.run_after = utcnow()
    await db_session.commit()
    claimed = await claim(db_session)
    await AssistantApprovalExecuteHandler().run(build_ctx(db_engine, claimed))
    done = await tasks.get(db_session, claimed.id)
    assert done is not None and done.status == "succeeded"
    assert (await reload(db_session, row)).status == "executed" and len(feishu.writes) == 1


async def approved_file(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> tuple[AssistantApproval, Task, FakeFeishu, FakeTower, FakeRelay, FakeBucket]:
    """一张已批准的「保存文件到云空间」，文件在临时桶里。"""
    feishu, fake = install(monkeypatch)
    relay, bucket = install_files(monkeypatch)
    _, _, scope = await assistant_turn(session)
    arguments = {"upload_id": relay.add("报价单.pdf", b"%PDF quote"), "folder_token": "fld_1"}
    out = await approvals.submit(
        session,
        None,
        scope,
        "feishu_save_to_drive",
        arguments,
        cfg=CFG,
        files=relay,  # type: ignore[arg-type]
    )
    await session.commit()
    row = await session.get(AssistantApproval, out["approval_id"])
    assert row is not None
    await decided(session, row)
    task = (await queued(session, APPROVAL_EXECUTE_KIND))[0]
    return row, task, feishu, fake, relay, bucket


async def run_file(
    engine: AsyncEngine, row: AssistantApproval, task: Task, relay: FakeRelay | None
) -> str:
    return await execute.run(
        make_session_factory(engine),
        Cipher(MASTER),
        row.id,
        task,
        CFG,
        relay,  # type: ignore[arg-type]
    )


async def test_approved_file_write_uploads_the_staged_bytes(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, task, feishu, fake, relay, bucket = await approved_file(db_session, monkeypatch)
    assert await run_file(db_engine, row, task, relay) == "executed"
    # 传给飞书的就是提交时的那份文件，文件名与类型也一样。
    path, data, files = feishu.uploads[0]
    assert path == "/drive/v1/files/upload_all" and data["parent_node"] == "fld_1"
    assert files == {"file": ("报价单.pdf", b"%PDF quote", "application/pdf")}
    assert len(fake.redeemed) == 1 and fake.reported == [(row.approval_id, "executed", None)]
    # 执行路径不碰桶里的对象：有了结论之后由 scheduler 清理（Task 15）。
    assert len(bucket.objects) == 1 and bucket.deleted == []
    assert (await reload(db_session, row)).has_files


async def test_missing_or_altered_attachment_is_never_sent(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, task, feishu, fake, relay, bucket = await approved_file(db_session, monkeypatch)
    (key,) = bucket.objects
    # 桶里的内容被换过（大小相同）：哈希对不上，不执行，也不用掉这次批准的令牌。
    bucket.objects[key] = b"%PDF fraud"
    assert await run_file(db_engine, row, task, relay) == "failed"
    row = await reload(db_session, row)
    assert row.error_code == "attachment_changed" and row.execute_started_at is None
    assert feishu.uploads == [] and fake.redeemed == []
    assert fake.reported == [(row.approval_id, "failed", "attachment_changed")]


async def test_swept_attachment_or_missing_relay_fails_closed(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, task, feishu, fake, relay, bucket = await approved_file(db_session, monkeypatch)
    # 桶暂时连不上：什么都不做，稍后重试；放弃时记下的是桶的原因。
    bucket.down = True
    with pytest.raises(execute.Retry) as retry:
        await run_file(db_engine, row, task, relay)
    assert retry.value.code == "attachment_store_unavailable"
    assert (await reload(db_session, row)).status == "approved" and fake.redeemed == []
    bucket.down = False
    # 对象已被清理（例如桶的自动清理先到）：不执行。
    bucket.objects.clear()
    assert await run_file(db_engine, row, task, relay) == "failed"
    assert (await reload(db_session, row)).error_code == "attachment_missing"
    assert feishu.uploads == [] and fake.redeemed == []


async def test_file_approval_without_a_relay_is_not_executed(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, task, feishu, fake, _, _ = await approved_file(db_session, monkeypatch)
    assert await run_file(db_engine, row, task, None) == "failed"
    assert (await reload(db_session, row)).error_code == "attachments_not_supported"
    assert feishu.uploads == [] and fake.redeemed == []


def second_commit(monkeypatch: pytest.MonkeyPatch, before: Any) -> None:
    """让 run() 里的第二次提交（调飞书之后那一次）先执行 `before`，原提交随后照常进行。"""
    original = AsyncSession.commit
    calls = {"n": 0}

    async def commit(self: AsyncSession) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            await before()
        await original(self)

    monkeypatch.setattr(AsyncSession, "commit", commit)


async def test_failed_commit_after_feishu_still_closes_the_approval(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, task, feishu, fake = await approved(db_session, monkeypatch)

    async def boom() -> None:
        raise RuntimeError("commit broke")

    second_commit(monkeypatch, boom)
    # 飞书已经发出去了；记录令牌状态的那次提交失败也不能让单悬着。
    assert await run(db_engine, row, task) == "executed"
    assert len(feishu.writes) == 1
    row = await reload(db_session, row)
    assert row.status == "executed"
    assert fake.reported == [(row.approval_id, "executed", None)]
    assert [t.payload["status"] for t in await queued(db_session, APPROVAL_RESULT_KIND)] == [
        "executed"
    ]


async def test_closed_by_someone_else_meanwhile_is_not_closed_twice(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, task, _, fake = await approved(db_session, monkeypatch)

    async def other() -> None:
        async with db_engine.begin() as conn:
            await conn.execute(
                text("UPDATE assistant_approvals SET status='failed', error_code='x' WHERE id=:i"),
                {"i": row.id},
            )

    second_commit(monkeypatch, other)
    assert await run(db_engine, row, task) == "failed"
    assert fake.reported == [] and await queued(db_session, APPROVAL_RESULT_KIND) == []


async def age(session: AsyncSession, row: AssistantApproval, column: str, minutes: int) -> None:
    setattr(row, column, utcnow() - timedelta(minutes=minutes))
    await session.commit()


async def set_task_status(session: AsyncSession, task: Task, status: str) -> None:
    await session.execute(
        text("UPDATE tasks SET status=:s WHERE id=:i"), {"s": status, "i": task.id}
    )
    await session.commit()


async def test_stuck_approval_without_a_live_task_is_closed_and_reported(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, task, feishu, fake = await approved(db_session, monkeypatch)
    await age(db_session, row, "decided_at", 20)
    await set_task_status(db_session, task, "failed")
    assert await execute.recover_stuck(db_session, utcnow(), CFG) == 1
    await db_session.commit()
    row = await reload(db_session, row)
    assert (row.status, row.error_code) == ("failed", "execution_not_started")
    assert fake.reported == [(row.approval_id, "failed", "execution_not_started")]
    assert [t.payload["status"] for t in await queued(db_session, APPROVAL_RESULT_KIND)] == [
        "failed"
    ]
    assert feishu.writes == []
    assert await execute.recover_stuck(db_session, utcnow(), CFG) == 0


async def test_stuck_check_leaves_live_or_recent_approvals_alone(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, task, _, fake = await approved(db_session, monkeypatch)
    # 任务还在排队：不动，哪怕批准已经过了很久。
    await age(db_session, row, "decided_at", 20)
    assert await execute.recover_stuck(db_session, utcnow(), CFG) == 0
    # 任务没了，但才批准两分钟：也不动。
    await age(db_session, row, "decided_at", 2)
    await set_task_status(db_session, task, "failed")
    assert await execute.recover_stuck(db_session, utcnow(), CFG) == 0
    assert (await reload(db_session, row)).status == "approved" and fake.reported == []


async def test_stuck_after_execution_started_counts_from_the_start(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, task, _, fake = await approved(db_session, monkeypatch)
    await age(db_session, row, "execute_started_at", 20)
    await set_task_status(db_session, task, "failed")
    assert await execute.recover_stuck(db_session, utcnow(), None) == 1
    await db_session.commit()
    assert (await reload(db_session, row)).error_code == "execution_interrupted"
    assert fake.reported == []


@pytest.mark.parametrize("state", ["deprovisioning", "provisioning", "deprovisioned", None])
async def test_approval_decided_after_the_assistant_left_active_is_never_queued(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, state: str | None
) -> None:
    feishu, fake = install(monkeypatch)
    bot, _, scope = await assistant_turn(db_session)
    row = await pending(db_session, scope)
    # 停用已经开始（记录仍是 enabled，直到最后一步）：之后送达的批准不执行。
    bot.assistant_state = state
    await db_session.commit()
    found = await decided(db_session, row)
    assert found.known and not found.queued
    row = await reload(db_session, row)
    assert (row.status, row.error_code) == ("withdrawn", "assistant_inactive")
    assert await queued(db_session, APPROVAL_EXECUTE_KIND) == []
    assert await queued(db_session, APPROVAL_RESULT_KIND) == []
    assert feishu.writes == [] and fake.redeemed == []


async def test_approved_while_active_still_executes_during_the_stop(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, task, feishu, _ = await approved(db_session, monkeypatch)
    await db_session.execute(
        text("UPDATE bots SET assistant_state = 'deprovisioning' WHERE id = :id"),
        {"id": row.bot_id},
    )
    await db_session.commit()
    assert await run(db_engine, row, task) == "executed"
    assert len(feishu.writes) == 1

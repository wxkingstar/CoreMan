"""第三期：账本镜像（API 任务）、截止提醒、记要带上与统计。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from coreman.core.bus import tasks
from coreman.core.db.models import (
    ApiTask,
    OutboxItem,
    PersonalAccessToken,
    Setting,
    User,
    UserIdentity,
    WorkTask,
)
from coreman.core.work_tasks import cards, due, ledger, service
from coreman.core.work_tasks.service import WorkTaskError
from tests.integration.worker_helpers import seed_bot

BASE = "https://coreman.example.com"


async def _alice(db_session) -> User:
    alice = User(login_name="alice", display_name="alice", email="alice@example.test")
    db_session.add(alice)
    await db_session.flush()
    db_session.add(UserIdentity(user_id=alice.id, platform="feishu", platform_user_id="ou_a"))
    return alice


async def test_api_tasks_are_mirrored_read_only_and_follow_their_turn(db_session):
    bot, _, _ = await seed_bot(db_session)
    bot.platform = "feishu"
    alice = await _alice(db_session)
    token = PersonalAccessToken(
        user_id=alice.id,
        name="t",
        token_prefix="cm_",
        token_hash="h",
        bot_ids=[bot.id],
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(token)
    await db_session.flush()
    turn = await tasks.enqueue(
        db_session, tasks.NewTask(bot_id=bot.id, kind="chat", user_id=alice.id, session_key="oc")
    )
    api = ApiTask(
        token_id=token.id,
        user_id=alice.id,
        bot_id=bot.id,
        prompt="统计上周退货率\n按渠道拆开",
        target="p2p",
        chat_id="oc_dm",
        task_id=turn.id,
    )
    db_session.add(api)
    await db_session.commit()

    now = datetime.now(UTC)
    assert await ledger.sync(db_session, now) >= 1
    await db_session.commit()
    mirror = await db_session.scalar(select(WorkTask).where(WorkTask.origin_ref == f"api:{api.id}"))
    # 回到私聊的 API 任务：只记谁发起、状态，不复制内容。
    assert mirror.title == "API发起的私聊任务" and mirror.brief == "" and mirror.source == "api"
    assert mirror.status == "in_progress" and mirror.principal_user_id == alice.id
    # 没有变化时不重复改。
    assert await ledger.sync(db_session, now) == 0
    # 镜像只读：不能在任务页转交、取消。
    try:
        await service.cancel(db_session, mirror, alice, "不要了")
    except WorkTaskError as exc:
        assert "原处" in exc.message
    else:
        raise AssertionError("mirror should be read-only")
    # 那一轮做完（哪怕原记录已经过了 7 天）：镜像跟着完成；再同步一次不重复建。
    turn.status = "succeeded"
    api.created_at = now - timedelta(days=10)
    await db_session.commit()
    await ledger.sync(db_session, now)
    await db_session.commit()
    mirrors = list(await db_session.scalars(select(WorkTask).where(WorkTask.source == "api")))
    assert len(mirrors) == 1 and mirrors[0].status == "done"


async def test_due_reminders_go_once_and_cards_postpone(db_session):
    bot, _, _ = await seed_bot(db_session)
    bot.platform = "feishu"
    alice = await _alice(db_session)
    db_session.add(Setting(key=cards.NOTIFIER_SETTING, value=str(bot.id)))
    now = datetime.now(UTC)
    row = await service.create(
        db_session, alice, title="交季度预算", due_at=now + timedelta(hours=3)
    )
    await db_session.commit()
    assert await due.tick(db_session, now, BASE) == 1
    assert await due.tick(db_session, now, BASE) == 0
    await db_session.commit()
    sent = await db_session.scalar(select(OutboxItem).where(OutboxItem.kind == "send"))
    assert sent.target == {"user_id": "ou_a"} and "快到期" in str(sent.payload["card"])
    assert "延期 1 天" in str(sent.payload["card"])
    # 过了截止：再提醒一次「已超期」。
    assert await due.tick(db_session, now + timedelta(hours=4), BASE) == 1
    await db_session.commit()
    # 延期后重新计。
    due.postpone(row, 1, now + timedelta(hours=4))
    assert row.overdue_notified_at is None and row.due_at > now + timedelta(hours=20)

"""第三期：账本镜像（API 任务）、截止提醒、记要带上与统计。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from coreman.core.bus import tasks
from coreman.core.db.models import (
    ApiClient,
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


async def test_records_name_the_calling_system_and_keep_markdown_out_of_the_one_liner(db_session):
    bot, _, _ = await seed_bot(db_session)
    bot.platform = "feishu"
    alice = await _alice(db_session)
    db_session.add(ApiClient(app_key="ops-monitor", name="监控系统", secret_enc="x"))
    await db_session.flush()
    api = ApiTask(
        client_key="ops-monitor",
        request_id="r1",
        user_id=alice.id,
        bot_id=bot.id,
        prompt="## 排查告警\n看一下磁盘",
        target="p2p",
        chat_id="oc_dm",
    )
    db_session.add(api)
    await db_session.commit()
    await ledger.sync(db_session, datetime.now(UTC))
    await db_session.commit()
    mirror = await db_session.scalar(select(WorkTask).where(WorkTask.origin_ref == f"api:{api.id}"))
    # 写出是哪个业务系统：一排「业务系统发起的私聊任务」分不出谁是谁；私聊的内容仍然不抄。
    assert mirror.title == "监控系统发起的私聊任务" and mirror.summary == "alice经监控系统发起"
    assert mirror.brief == ""

    # AI 的回复原文带加粗、标题、列表和链接：给人看的一句话里不留这些记号。
    assert (
        service.plain_line(
            "**结论：**发货回传在 `test` 环境走通了\n- 见 [发布记录](https://example.com/r/1)"
        )
        == "结论：发货回传在 test 环境走通了 见 发布记录"
    )
    assert ledger._title("# 排查告警\n看一下磁盘", "求助：") == "求助：排查告警"


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


async def test_escalations_are_mirrored_without_their_content(db_session):
    from coreman.core.crypto import Cipher
    from coreman.core.db.models import ApiClient, Escalation
    from tests.integration.worker_helpers import MASTER

    bot, _, _ = await seed_bot(db_session)
    alice = await _alice(db_session)
    db_session.add(
        ApiClient(
            app_key="skill",
            name="技能",
            scopes=["escalations"],
            secret_enc=Cipher(MASTER).encrypt("s", "api_clients.secret_enc"),
        )
    )
    await db_session.flush()
    now = datetime.now(UTC)
    row = Escalation(
        escalation_id="e1",
        owner_client_key="skill",
        request_fingerprint="f",
        to_platform_user_id="ou_a",
        bot_id=bot.id,
        to_user_id=alice.id,
        notify_platform="feishu_bot",
        question="线上库存对不上，要不要回滚？",
        status="pending",
        expires_at=now + timedelta(minutes=30),
    )
    db_session.add(row)
    await db_session.commit()
    await ledger.sync(db_session, now)
    await db_session.commit()
    mirror = await db_session.scalar(
        select(WorkTask).where(WorkTask.origin_ref == f"escalation:{row.id}")
    )
    assert mirror.title == "人工求助：问alice" and mirror.brief == ""
    assert "库存" not in mirror.title + mirror.summary
    assert mirror.status == "waiting" and mirror.waiting_on["human_text"] == "等alice回复"
    assert mirror.assignee_user_id == alice.id
    row.status, row.resolution = "completed", "observed"
    await db_session.commit()
    await ledger.sync(db_session, now)
    await db_session.commit()
    mirror = await db_session.scalar(
        select(WorkTask)
        .where(WorkTask.origin_ref == f"escalation:{row.id}")
        .execution_options(populate_existing=True)
    )
    assert mirror.status == "done" and mirror.summary == "线下已解决"

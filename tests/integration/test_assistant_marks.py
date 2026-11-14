"""「处理中」表情：唯一不经审批的写操作，表情类型与目标消息由平台限定。"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants.approvals import marks
from coreman.core.bus import tasks
from coreman.core.db.models import AssistantJob, AssistantMark
from coreman.core.timeutils import utcnow
from tests.integration.approval_helpers import OWNER, FakeFeishu, assistant_turn, install, message

pytestmark = pytest.mark.usefixtures("assistant_master")


async def test_mark_and_unmark_someone_elses_message(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    feishu, fake = install(monkeypatch)
    bot, _, scope = await assistant_turn(db_session)
    assert await marks.mark(db_session, None, scope, "om_2", done=False) == {"status": "marked"}  # type: ignore[arg-type]
    await db_session.commit()
    # 表情类型是平台定的，不来自助理。
    assert feishu.writes == [
        ("POST", "/im/v1/messages/om_2/reactions", {"reaction_type": {"emoji_type": "OnIt"}})
    ]
    row = await db_session.get(AssistantMark, (bot.id, "om_2"))
    assert row is not None and (row.chat_id, row.reaction_id) == ("oc_li", "r_1")
    # 没有提交任何审批单。
    assert fake.submitted == []
    # 重复标记不再调飞书；撤销只撤平台记下的那一个。
    assert (await marks.mark(db_session, None, scope, "om_2", done=False))["status"] == "marked"  # type: ignore[arg-type]
    assert len(feishu.writes) == 1
    assert await marks.mark(db_session, None, scope, "om_2", done=True) == {"status": "unmarked"}  # type: ignore[arg-type]
    await db_session.commit()
    assert feishu.writes[1][:2] == ("DELETE", "/im/v1/messages/om_2/reactions/r_1")
    assert await db_session.get(AssistantMark, (bot.id, "om_2")) is None
    assert await marks.mark(db_session, None, scope, "om_2", done=True) == {"status": "not_marked"}  # type: ignore[arg-type]


async def test_only_live_messages_from_other_people_can_be_marked(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = [
        message("om_own", OWNER, "我发的"),
        message("om_gone", deleted=True),
        message("om_bot", "cli_example", kind="app"),
    ]
    feishu, _ = install(monkeypatch, FakeFeishu(history))
    _, _, scope = await assistant_turn(db_session)
    for target in ("om_own", "om_gone", "om_bot", "om_missing"):
        out = await marks.mark(db_session, None, scope, target, done=False)  # type: ignore[arg-type]
        assert out["error"] == "not_a_pending_message", target
    assert feishu.writes == []


async def test_marks_are_capped(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    feishu, _ = install(monkeypatch)
    bot, _, scope = await assistant_turn(db_session)
    db_session.add_all(
        AssistantMark(bot_id=bot.id, message_id=f"om_old_{i}", chat_id="oc_li", reaction_id="r")
        for i in range(marks.MAX_MARKS)
    )
    await db_session.flush()
    out = await marks.mark(db_session, None, scope, "om_2", done=False)  # type: ignore[arg-type]
    assert out["error"] == "too_many_marks" and feishu.writes == []


async def test_a_reaction_without_an_id_is_not_stored(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    feishu, _ = install(monkeypatch)
    feishu.reaction_id = None
    bot, _, scope = await assistant_turn(db_session)
    out = await marks.mark(db_session, None, scope, "om_2", done=False)  # type: ignore[arg-type]
    assert out == {"error": "mark_failed"}
    assert await db_session.get(AssistantMark, (bot.id, "om_2")) is None


class Reactions:
    """以所有者身份调飞书的替身：只记下撤了哪些表情。"""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def __call__(self, key: str, **kw: Any) -> dict[str, Any]:
        assert key == "im.reaction_delete"
        self.deleted.append(kw["path"]["message_id"])
        return {}


async def test_the_platform_lifts_marks_when_the_investigation_ends(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """这一轮结束时撤掉它打的；交给子任务接着查的，等结论交回之后再撤；升级前的不动。"""
    install(monkeypatch)
    bot, _, scope = await assistant_turn(db_session)
    assert (await marks.mark(db_session, None, scope, "om_2", done=False))["status"] == "marked"  # type: ignore[arg-type]
    db_session.add_all(
        [
            AssistantMark(
                bot_id=bot.id,
                message_id="om_quick",
                chat_id="oc_li",
                reaction_id="r_q",
                task_id=scope.task.id,
            ),
            AssistantMark(bot_id=bot.id, message_id="om_legacy", chat_id="oc_li", reaction_id="r"),
        ]
    )
    await db_session.commit()
    reactions = Reactions()
    # 那一轮还在跑：不撤。
    assert await marks.release_due(db_session, reactions, bot.id) == 0
    job = AssistantJob(
        bot_id=bot.id,
        title_enc="查报价",
        brief_enc="查一下",
        session_key="oc_assistant#job:1",
        origin_session_key="oc_assistant",
        deadline_at=utcnow() + timedelta(hours=1),
    )
    db_session.add(job)
    await db_session.flush()
    # 这一轮把 om_2 交给子任务接着查；om_quick 是更早一轮打的（任务行已清理），忘了撤。
    await db_session.execute(
        update(AssistantMark).where(AssistantMark.message_id == "om_quick").values(task_id=-1)
    )
    assert await marks.bind_job(db_session, bot.id, scope.task.id, job.id) == 1
    await tasks.finish(db_session, scope.task.id, status="succeeded")
    await db_session.commit()
    assert await marks.release_due(db_session, reactions, bot.id) == 1
    await db_session.commit()
    assert reactions.deleted == ["om_quick"]
    # 子任务的结论交回之后才撤 om_2。
    job.reported_at = utcnow()
    await db_session.commit()
    assert await marks.release_due(db_session, reactions, bot.id) == 1
    await db_session.commit()
    assert reactions.deleted == ["om_quick", "om_2"]
    assert await db_session.get(AssistantMark, (bot.id, "om_2")) is None
    # 升级前打的没有记那一轮：只按时间提醒，平台不撤。
    assert await db_session.get(AssistantMark, (bot.id, "om_legacy")) is not None

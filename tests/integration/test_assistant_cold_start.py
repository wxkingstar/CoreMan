"""冷启动的状态机：开始、四张卡片与令牌、作答与跳过、完成，每一步幂等。"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import cold_start, notes, settings
from coreman.core.crypto import Cipher
from coreman.core.db.models import AssistantSettings, Bot, OutboxItem
from coreman.core.timeutils import utcnow
from tests.integration.assistant_helpers import SECRET, raw
from tests.integration.lifecycle_helpers import active_assistant
from tests.integration.shift_helpers import last_sent
from tests.integration.test_assistant_platform_turn_handler import assistant
from tests.integration.worker_helpers import MASTER

pytestmark = pytest.mark.usefixtures("assistant_master")
CIPHER = Cipher(MASTER)
AUTHOR = "SELECT updated_by FROM assistant_pages WHERE bot_id = :bot"


async def started(session: AsyncSession, step: str = "people") -> tuple[Bot, AssistantSettings]:
    """冷启动已开始、走到 `step` 的助理。"""
    bot, _ = await assistant(session)
    assert await cold_start.begin(session, bot.id, utcnow())
    row = await settings.get(session, bot.id)
    row.cold_start_step = step
    await session.commit()
    return bot, row


async def sent_count(session: AsyncSession, bot: Bot) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(OutboxItem)
            .where(
                OutboxItem.bot_id == bot.id,
                OutboxItem.dedupe_key.like("assistant-cold-start:%"),
            )
        )
        or 0
    )


def form(card: dict[str, Any]) -> dict[str, Any]:
    return next(item for item in card["body"]["elements"] if item.get("tag") == "form")


async def test_begin_starts_once(db_session: AsyncSession) -> None:
    bot, _ = await assistant(db_session)
    now = utcnow()
    assert await cold_start.begin(db_session, bot.id, now)
    row = await settings.get(db_session, bot.id)
    assert (row.cold_start_started_at, row.cold_start_step, row.cold_start_next_at) == (
        now,
        "voice",
        now,
    )
    assert cold_start.pending(row) and settings.cold_start_state(row) == "in_progress"
    # 再开始一次什么都不变。
    assert not await cold_start.begin(db_session, bot.id, now + timedelta(hours=1))
    assert row.cold_start_started_at == now


async def test_begin_needs_a_recorded_identity_and_an_unfinished_start(
    db_session: AsyncSession,
) -> None:
    bot, _, _ = await active_assistant(db_session)
    # 没记下身份（迁移 0093 之前开通的）：没有设置行，不开始，也不建行。
    assert not await cold_start.begin(db_session, bot.id, utcnow())
    assert await db_session.get(AssistantSettings, bot.id) is None
    assert not cold_start.pending(None)


async def test_begin_does_nothing_once_done(db_session: AsyncSession) -> None:
    bot, row = await started(db_session)
    assert await cold_start.complete(db_session, bot, row, "skipped", utcnow())
    assert not await cold_start.begin(db_session, bot.id, utcnow())
    assert row.cold_start_step is None and not cold_start.pending(row)


async def test_each_question_has_its_own_card_and_tokens(db_session: AsyncSession) -> None:
    bot, row = await started(db_session)
    groups = [("oc_team", "报价群"), ("oc_ops", "x" * 80)]
    names = {
        "people": ["answer"],
        "duties": ["answer"],
        "focus": ["focus"],
        "hours": ["hours", "timezone"],
    }
    for index, step in enumerate(cold_start.QUESTIONS, start=1):
        card = cold_start.card(CIPHER, bot.id, row, step, groups=groups)
        assert card["schema"] == "2.0"
        assert card["task_id"] == f"personal:{cold_start.nonce(row)}"
        assert card["header"]["title"]["content"] == f"认识一下（{index}/4）"
        fields = form(card)["elements"]
        assert [item["name"] for item in fields[:-1]] == names[step]
        submit = fields[-1]
        assert submit["form_action_type"] == "submit"
        value = submit["behaviors"][0]["value"]
        assert value["task_id"] == card["task_id"]
        assert value["event_key"] == cold_start.EVENT_KEY
        assert cold_start.parse(CIPHER, bot.id, row, value["level"]) == (step, "answer")
        skip = next(
            item
            for item in card["body"]["elements"]
            if item.get("tag") == "button" and item["text"]["content"] == "跳过这一题"
        )
        level = skip["behaviors"][0]["value"]["level"]
        assert cold_start.parse(CIPHER, bot.id, row, level) == (step, "skip")
    focus = form(cold_start.card(CIPHER, bot.id, row, "focus", groups=groups))["elements"][0]
    assert focus["tag"] == "multi_select_static"
    assert [option["value"] for option in focus["options"]] == ["oc_team", "oc_ops"]
    # 群名是别人起的：只截短、只放进纯文本。
    assert focus["options"][1]["text"] == {"tag": "plain_text", "content": "x" * 30}
    hours = form(cold_start.card(CIPHER, bot.id, row, "hours"))["elements"]
    assert hours[0]["tag"] == hours[1]["tag"] == "select_static"
    assert "09:30-19:00" in [option["value"] for option in hours[0]["options"]]
    assert "Asia/Shanghai" in [option["value"] for option in hours[1]["options"]]


async def test_tokens_are_bound_to_the_record_the_start_the_step_and_the_action(
    db_session: AsyncSession,
) -> None:
    bot, row = await started(db_session)
    good = cold_start.token(CIPHER, bot.id, row, "people", "answer")
    step, verb, mac = good.split(".")
    assert cold_start.parse(CIPHER, bot.id, row, good) == ("people", "answer")
    for forged in (
        f"duties.{verb}.{mac}",  # 换了一题
        f"{step}.skip.{mac}",  # 换了动作
        f"{step}.{verb}.{'0' * 64}",
        f"{step}.{verb}.停用",  # 非 ASCII 不能让比较抛错
        f"voice.{verb}.{mac}",  # 不是问题的步骤
        "people",
        "",
    ):
        assert cold_start.parse(CIPHER, bot.id, row, forged) is None, forged
    # 别的记录、别的主密钥签的都不认。
    assert cold_start.parse(CIPHER, uuid.uuid4(), row, good) is None
    assert cold_start.parse(Cipher(b"\x09" * 32), bot.id, row, good) is None
    # 重新开始过（开始时刻变了）：旧卡片作废。
    row.cold_start_started_at = row.cold_start_started_at + timedelta(seconds=1)
    assert cold_start.parse(CIPHER, bot.id, row, good) is None


async def test_send_is_idempotent_and_hands_the_turn_to_the_owner(
    db_session: AsyncSession,
) -> None:
    bot, row = await started(db_session)
    now = utcnow()
    await cold_start.send(db_session, CIPHER, bot, row, "people", now)
    await cold_start.send(db_session, CIPHER, bot, row, "people", now)
    await db_session.commit()
    assert await sent_count(db_session, bot) == 1
    assert row.cold_start_next_at is None and row.cold_start_step == "people"
    sent = await last_sent(db_session, bot)
    assert sent is not None and sent.target == {"chat_id": "oc_private"}
    assert sent.payload["card"]["task_id"] == f"personal:{cold_start.nonce(row)}"


async def test_an_answer_becomes_a_pinned_page_written_as_the_owner(
    db_session: AsyncSession,
) -> None:
    bot, row = await started(db_session)
    now = utcnow()
    said = await cold_start.answer(
        db_session, bot, row, "people", {"answer": [f"  上级是李四，{SECRET}  "]}, now
    )
    await db_session.commit()
    assert said == cold_start.SAVED.format(path="profile/people.md") and SECRET not in said
    page = await notes.read(db_session, bot.id, "profile/people.md")
    assert page is not None and page.pinned and page.body == f"上级是李四，{SECRET}"
    assert page.title == "上级与主要协作人"
    assert await raw(db_session, AUTHOR, bot=bot.id) == "owner"
    # 前进到下一题，轮到平台出手。
    assert (row.cold_start_step, row.cold_start_next_at) == ("duties", now)
    # 同一题再答一次（重做冷启动、页已存在）：整页替换，不报错。
    row.cold_start_step = "people"
    await cold_start.answer(db_session, bot, row, "people", {"answer": ["上级是王五"]}, now)
    page = await notes.read(db_session, bot.id, "profile/people.md")
    assert page is not None and page.body == "上级是王五" and page.version == 2


async def test_an_empty_or_overlong_answer(db_session: AsyncSession) -> None:
    bot, row = await started(db_session, "duties")
    now = utcnow()
    assert await cold_start.answer(db_session, bot, row, "duties", {}, now) == cold_start.NOT_SAVED
    assert await notes.read(db_session, bot.id, "profile/duties.md") is None
    assert row.cold_start_step == "focus"
    row.cold_start_step = "duties"
    await cold_start.answer(db_session, bot, row, "duties", {"answer": ["报" * 900]}, now)
    page = await notes.read(db_session, bot.id, "profile/duties.md")
    assert page is not None and len(page.body) == cold_start.MAX_ANSWER


async def test_skip_moves_on_without_writing(db_session: AsyncSession) -> None:
    bot, row = await started(db_session)
    now = utcnow()
    await cold_start.skip(db_session, bot, row, "people", now)
    assert (row.cold_start_step, row.cold_start_next_at) == ("duties", now)
    assert await notes.read(db_session, bot.id, "profile/people.md") is None


async def test_focus_chats_go_to_the_settings(db_session: AsyncSession) -> None:
    bot, row = await started(db_session, "focus")
    now = utcnow()
    said = await cold_start.answer(
        db_session, bot, row, "focus", {"focus": ["oc_team", "oc_ops"]}, now
    )
    await db_session.commit()
    assert said == "已把 2 个群设为重点群。"
    assert settings.view(row, now)["focus_chats"] == ["oc_team", "oc_ops"]
    assert row.cold_start_step == "hours"
    # 一个都没选、或值不像会话 id：不写名单，照样前进。
    row.cold_start_step = "focus"
    assert await cold_start.answer(db_session, bot, row, "focus", {"focus": []}, now) == (
        "没有选重点群。"
    )
    row.cold_start_step = "focus"
    bad = await cold_start.answer(db_session, bot, row, "focus", {"focus": ["om_x"]}, now)
    assert bad == cold_start.NOT_SAVED
    assert settings.view(row, now)["focus_chats"] == ["oc_team", "oc_ops"]


async def test_hours_and_timezone_finish_the_cold_start(db_session: AsyncSession) -> None:
    bot, row = await started(db_session, "hours")
    now = utcnow()
    said = await cold_start.answer(
        db_session,
        bot,
        row,
        "hours",
        {"hours": ["10:00-20:00"], "timezone": ["Asia/Tokyo"]},
        now,
    )
    await db_session.commit()
    assert said == "工作时段 10:00–20:00，时区 Asia/Tokyo。"
    assert (row.work_start, row.work_end, row.timezone) == ("10:00", "20:00", "Asia/Tokyo")
    # 最后一题答完：冷启动完成。
    assert row.cold_start_done_at == now and row.cold_start_outcome == "completed"
    assert row.cold_start_step is None and row.cold_start_next_at is None
    assert settings.cold_start_state(row) == "done"
    sent = await last_sent(db_session, bot)
    assert sent is not None and sent.target == {"chat_id": "oc_private"}
    text = sent.payload["markdown"]
    assert "10:00–20:00" in text and "Asia/Tokyo" in text and "周一、周二" in text
    assert "上班" in text and "下班" in text


async def test_values_not_on_the_card_are_ignored(db_session: AsyncSession) -> None:
    bot, row = await started(db_session, "hours")
    said = await cold_start.answer(
        db_session,
        bot,
        row,
        "hours",
        {"hours": ["00:00-23:59"], "timezone": ["Mars/Olympus"]},
        utcnow(),
    )
    assert said == cold_start.NOT_SAVED
    assert (row.work_start, row.work_end, row.timezone) == ("09:30", "19:00", "Asia/Shanghai")
    assert row.cold_start_outcome == "completed"


async def test_complete_is_idempotent_and_says_how_it_ended(db_session: AsyncSession) -> None:
    bot, row = await started(db_session)
    now = utcnow()
    assert await cold_start.complete(db_session, bot, row, "timeout", now)
    assert not await cold_start.complete(db_session, bot, row, "completed", now + timedelta(1))
    await db_session.commit()
    assert row.cold_start_done_at == now and row.cold_start_outcome == "timeout"
    assert await sent_count(db_session, bot) == 1
    sent = await last_sent(db_session, bot)
    assert sent is not None and "24 小时" in sent.payload["markdown"]
    assert "09:30–19:00" in cold_start.done_text(row, "skipped")
    assert cold_start.done_text(row, "skipped").startswith("好的，先跳过。")
    assert cold_start.done_text(row, "completed").startswith("认识完了。")


async def test_stop_clears_an_unfinished_cold_start_and_the_switch(
    db_session: AsyncSession,
) -> None:
    bot, row = await started(db_session, "focus")
    settings.clock_in(row, utcnow())
    row.cold_start_next_at = utcnow()
    await cold_start.stop(db_session, bot.id)
    assert row.cold_start_step is None and row.cold_start_next_at is None
    assert row.shift_override is None and row.shift_override_until is None
    assert row.cold_start_done_at is None
    # 没有设置行的记录：什么都不做。
    await cold_start.stop(db_session, uuid.uuid4())

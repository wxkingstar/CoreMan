"""冷启动的循环：拉样本、逐题发卡片、失败重试与放弃、24 小时兜底；每一步重复执行不多做。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from coreman.core.assistants import cold_start, settings
from coreman.core.assistants import watch_feed as feed
from coreman.core.assistants.kinds import COLD_START_KIND
from coreman.core.crypto import Cipher
from coreman.core.db.models import AssistantSettings, Bot, OutboxItem
from coreman.core.db.session import make_session_factory
from coreman.core.feishu_personal.service import PersonalError
from coreman.core.feishu_personal.toolbase import Call
from coreman.core.timeutils import utcnow
from tests.integration.shift_helpers import last_sent, tasks_of
from tests.integration.test_assistant_platform_turn_handler import assistant
from tests.integration.worker_helpers import MASTER

pytestmark = pytest.mark.usefixtures("assistant_master")
CIPHER = Cipher(MASTER)
READER = object()
SAMPLES = ["收到，我看一下", "可以，周四下班前给你", "这个先放一放", "辛苦了", "明天上午对一下"]


@dataclass
class Feishu:
    """以所有者身份读飞书的替身：样本、群清单，可以让它失败或让身份核对不过。"""

    samples: list[str] = field(default_factory=lambda: list(SAMPLES))
    groups: list[cold_start.Group] = field(default_factory=lambda: [("oc_team", "报价群")])
    error: Exception | None = None
    authorized: bool = True
    calls: list[str] = field(default_factory=list)
    # 给了就在读飞书的半路停住，等用例放行（模拟一次还没返回的调用）。
    hold: asyncio.Event | None = None
    reached: asyncio.Event = field(default_factory=asyncio.Event)


@pytest.fixture
def feishu(monkeypatch: pytest.MonkeyPatch) -> Feishu:
    fake = Feishu()

    async def reader(session: Any, cipher: Any, bot: Any, row: Any) -> Any:
        return READER if fake.authorized else None

    async def collect(call: Any, prefs: Any, now: datetime, *, ours: Any) -> list[str]:
        # 冷启动总是带上「哪些消息是平台代发的」这个查询。
        assert call is READER and await ours(["om_x"]) == frozenset()
        fake.calls.append("collect")
        if fake.hold is not None:
            fake.reached.set()
            await fake.hold.wait()
        if fake.error is not None:
            raise fake.error
        return list(fake.samples)

    async def groups(call: Any, row: Any) -> list[cold_start.Group]:
        assert call is READER
        fake.calls.append("groups")
        if fake.error is not None:
            raise fake.error
        return list(fake.groups)

    monkeypatch.setattr(cold_start, "_reader", reader)
    monkeypatch.setattr(cold_start.voice, "collect", collect)
    monkeypatch.setattr(cold_start, "_groups", groups)
    return fake


async def begun(session: AsyncSession) -> tuple[Bot, datetime]:
    bot, _ = await assistant(session)
    start = utcnow()
    assert await cold_start.begin(session, bot.id, start)
    await session.commit()
    return bot, start


async def row_of(session: AsyncSession, bot: Bot) -> AssistantSettings:
    row = await session.get(AssistantSettings, bot.id, populate_existing=True)
    assert row is not None
    return row


async def cards(session: AsyncSession, bot: Bot) -> int:
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


async def reply(
    session: AsyncSession, bot: Bot, step: str, selected: dict[str, list[str]] | None
) -> None:
    """所有者处理了这一题（`selected` 为 None 表示跳过这一题）。"""
    row = await row_of(session, bot)
    assert row.cold_start_step == step and row.cold_start_next_at is None
    if selected is None:
        await cold_start.skip(session, bot, row, step, utcnow())
    else:
        await cold_start.answer(session, bot, row, step, selected, utcnow())
    await session.commit()


async def test_the_whole_cold_start_step_by_step(
    db_engine: AsyncEngine, db_session: AsyncSession, feishu: Feishu
) -> None:
    factory: async_sessionmaker[AsyncSession] = make_session_factory(db_engine)
    bot, start = await begun(db_session)

    async def go() -> dict[str, int]:
        return await cold_start.tick(factory, CIPHER, utcnow())

    # 第一步：拉所有者自己发出的消息，排一轮口吻样本；不等它，直接往下问。
    assert await go() == {"voice_queued": 1}
    (voice_task,) = await tasks_of(db_session, bot, COLD_START_KIND)
    assert voice_task.payload["samples"] == SAMPLES
    assert voice_task.session_key == "oc_private#job:cold-start"
    assert await go() == {"asked": 1}
    # 卡片发出后等所有者：循环空转，不重发。
    assert await go() == {} and await cards(db_session, bot) == 1
    await reply(db_session, bot, "people", {"answer": ["上级是李四"]})
    assert await go() == {"asked": 1}
    await reply(db_session, bot, "duties", None)
    assert await go() == {"asked": 1}
    sent = await last_sent(db_session, bot)
    assert sent is not None and "报价群" in str(sent.payload["card"])
    await reply(db_session, bot, "focus", {"focus": ["oc_team"]})
    assert await go() == {"asked": 1}
    row = await row_of(db_session, bot)
    assert settings.cold_start_state(row) == "in_progress"
    await reply(db_session, bot, "hours", {"hours": ["09:30-19:00"], "timezone": ["Asia/Shanghai"]})
    row = await row_of(db_session, bot)
    assert row.cold_start_done_at is not None and row.cold_start_outcome == "completed"
    assert settings.view(row, utcnow())["focus_chats"] == ["oc_team"]
    # 四张卡片加一条结束语；之后循环不再碰它。
    assert await cards(db_session, bot) == 5
    assert await go() == {}
    assert feishu.calls == ["collect", "groups"]
    assert len(await tasks_of(db_session, bot, COLD_START_KIND)) == 1
    assert row.cold_start_started_at == start


async def test_too_few_samples_skip_the_voice_page(
    db_engine: AsyncEngine, db_session: AsyncSession, feishu: Feishu
) -> None:
    factory = make_session_factory(db_engine)
    bot, _ = await begun(db_session)
    feishu.samples = SAMPLES[:4]
    assert await cold_start.tick(factory, CIPHER, utcnow()) == {"voice_skipped": 1}
    assert await tasks_of(db_session, bot, COLD_START_KIND) == []
    assert (await row_of(db_session, bot)).cold_start_step == "people"


async def test_a_failed_fetch_is_retried_then_given_up(
    db_engine: AsyncEngine, db_session: AsyncSession, feishu: Feishu
) -> None:
    factory = make_session_factory(db_engine)
    bot, start = await begun(db_session)
    feishu.error = PersonalError("upstream_unavailable")
    assert await cold_start.tick(factory, CIPHER, start) == {"voice_retry": 1}
    row = await row_of(db_session, bot)
    assert row.cold_start_step == "voice"
    assert row.cold_start_next_at == start + cold_start.VOICE_RETRY
    # 没到重试的时刻不动。
    assert await cold_start.tick(factory, CIPHER, start + timedelta(minutes=5)) == {}
    assert await cold_start.tick(factory, CIPHER, start + timedelta(minutes=10)) == {
        "voice_retry": 1
    }
    # 满一小时还拉不到：放弃这一步，往下问。
    late = start + cold_start.VOICE_WINDOW
    assert await cold_start.tick(factory, CIPHER, late) == {"voice_skipped": 1}
    assert (await row_of(db_session, bot)).cold_start_step == "people"
    assert await tasks_of(db_session, bot, COLD_START_KIND) == []
    assert feishu.calls == ["collect", "collect", "collect"]


async def test_without_a_usable_authorization_nothing_is_read(
    db_engine: AsyncEngine, db_session: AsyncSession, feishu: Feishu
) -> None:
    factory = make_session_factory(db_engine)
    bot, _ = await begun(db_session)
    feishu.authorized = False
    assert await cold_start.tick(factory, CIPHER, utcnow()) == {"voice_skipped": 1}
    row = await row_of(db_session, bot)
    row.cold_start_step = "focus"
    await db_session.commit()
    # 列不出群：这一题自动跳过，直接到下一题。
    assert await cold_start.tick(factory, CIPHER, utcnow()) == {"focus_skipped": 1}
    assert (await row_of(db_session, bot)).cold_start_step == "hours"
    assert feishu.calls == [] and await cards(db_session, bot) == 0


async def test_no_groups_or_a_failed_listing_skips_the_focus_question(
    db_engine: AsyncEngine, db_session: AsyncSession, feishu: Feishu
) -> None:
    factory = make_session_factory(db_engine)
    bot, _ = await begun(db_session)
    for broken in ("empty", "error"):
        row = await row_of(db_session, bot)
        row.cold_start_step, row.cold_start_next_at = "focus", utcnow()
        await db_session.commit()
        feishu.groups = [] if broken == "empty" else [("oc_team", "报价群")]
        feishu.error = PersonalError("rate_limited") if broken == "error" else None
        assert await cold_start.tick(factory, CIPHER, utcnow()) == {"focus_skipped": 1}, broken
    assert await cards(db_session, bot) == 0


async def test_after_24_hours_it_completes_with_the_defaults(
    db_engine: AsyncEngine, db_session: AsyncSession, feishu: Feishu
) -> None:
    factory = make_session_factory(db_engine)
    bot, start = await begun(db_session)
    await cold_start.tick(factory, CIPHER, start)
    await cold_start.tick(factory, CIPHER, start)  # 第一题的卡片发出，等所有者
    row = await row_of(db_session, bot)
    assert row.cold_start_step == "people" and row.cold_start_next_at is None
    before = start + cold_start.DEADLINE - timedelta(seconds=1)
    assert await cold_start.tick(factory, CIPHER, before) == {}
    deadline = start + cold_start.DEADLINE
    assert await cold_start.tick(factory, CIPHER, deadline) == {"timeout": 1}
    row = await row_of(db_session, bot)
    assert row.cold_start_done_at == deadline and row.cold_start_outcome == "timeout"
    assert row.cold_start_step is None
    # 默认值原样。
    assert (row.work_start, row.work_end, row.timezone) == ("09:30", "19:00", "Asia/Shanghai")
    assert row.workdays == [1, 2, 3, 4, 5] and row.focus_chats_enc is None
    sent = await last_sent(db_session, bot)
    assert sent is not None and sent.payload["markdown"].startswith("开通满 24 小时")
    count = await cards(db_session, bot)
    # 再跑多少次都不多发。
    assert await cold_start.tick(factory, CIPHER, deadline + timedelta(hours=1)) == {}
    assert await cards(db_session, bot) == count


async def test_records_that_are_not_active_are_left_alone(
    db_engine: AsyncEngine, db_session: AsyncSession, feishu: Feishu
) -> None:
    factory = make_session_factory(db_engine)
    bot, start = await begun(db_session)
    bot.assistant_state = "deprovisioning"
    await db_session.commit()
    assert await cold_start.tick(factory, CIPHER, start + cold_start.DEADLINE) == {}
    assert (await row_of(db_session, bot)).cold_start_done_at is None
    assert feishu.calls == [] and await cards(db_session, bot) == 0


async def test_an_unexpected_error_pushes_the_row_back(
    db_engine: AsyncEngine, db_session: AsyncSession, feishu: Feishu
) -> None:
    factory = make_session_factory(db_engine)
    bot, start = await begun(db_session)
    feishu.error = RuntimeError("synthetic")
    assert await cold_start.tick(factory, CIPHER, start) == {"failed": 1}
    row = await row_of(db_session, bot)
    # 不是每 5 秒撞一次：推到十分钟之后。
    assert row.cold_start_step == "voice"
    assert row.cold_start_next_at == start + cold_start.VOICE_RETRY


async def test_unexpected_errors_never_hold_the_cold_start_up(
    db_engine: AsyncEngine, db_session: AsyncSession, feishu: Feishu
) -> None:
    """接口字段与假定不符之类的意外：口吻那一步拖满一小时放弃，列群出错直接跳过那一题。"""
    factory = make_session_factory(db_engine)
    bot, start = await begun(db_session)
    feishu.error = RuntimeError("synthetic")
    assert await cold_start.tick(factory, CIPHER, start) == {"failed": 1}
    late = start + cold_start.VOICE_WINDOW
    assert await cold_start.tick(factory, CIPHER, late) == {"failed": 1}
    row = await row_of(db_session, bot)
    assert (row.cold_start_step, row.cold_start_next_at) == ("people", late)
    row.cold_start_step = "focus"
    await db_session.commit()
    assert await cold_start.tick(factory, CIPHER, late) == {"focus_skipped": 1}
    assert (await row_of(db_session, bot)).cold_start_step == "hours"
    # 最后一题不读飞书：卡片照常发出。
    assert await cold_start.tick(factory, CIPHER, late) == {"asked": 1}


async def test_a_tick_cancelled_midway_finishes_its_assistant(
    db_engine: AsyncEngine, db_session: AsyncSession, feishu: Feishu
) -> None:
    """scheduler 停止时会取消循环：正在推进的这一位可能刚换过令牌，要等它提交完再退。"""
    factory = make_session_factory(db_engine)
    bot, start = await begun(db_session)
    feishu.hold = asyncio.Event()
    job = asyncio.create_task(cold_start.tick(factory, CIPHER, start))
    await asyncio.wait_for(feishu.reached.wait(), 5)
    job.cancel()
    await asyncio.sleep(0.05)
    assert not job.done()
    feishu.hold.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(job, 5)
    # 这一位的事务照常提交了：样本那一轮排上了，步骤前进了。
    assert (await row_of(db_session, bot)).cold_start_step == "people"
    assert len(await tasks_of(db_session, bot, COLD_START_KIND)) == 1


async def test_the_real_group_listing_keeps_named_groups_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def listed(call: Any) -> feed.Listed:
        return feed.Listed(
            ["oc_private", "oc_li", "oc_team", "oc_unnamed", "oc_ops"],
            {
                "oc_private": "",
                "oc_li": "",
                "oc_team": "报价群",
                "oc_unnamed": "",
                "oc_ops": "运维",
            },
            {"oc_private": "p2p", "oc_li": "p2p", "oc_team": "group"},
        )

    monkeypatch.setattr(cold_start.feed, "list_chats", listed)
    row = AssistantSettings(chat_id="oc_private")
    assert await cold_start._groups(object(), row) == [  # type: ignore[arg-type]
        ("oc_team", "报价群"),
        ("oc_ops", "运维"),
    ]


async def test_the_real_reader_acts_as_the_recorded_owner(db_session: AsyncSession) -> None:
    bot, _ = await begun(db_session)
    row = await row_of(db_session, bot)
    call = await cold_start._reader(db_session, CIPHER, bot, row)
    assert isinstance(call, Call) and call.scope.open_id == "ou_owner"
    assert call.scope.chat_id == "oc_private" and call.scope.bot.id == bot.id
    # 记下的身份与授权对不上：不调飞书。
    row.open_id = "ou_someone_else"
    assert await cold_start._reader(db_session, CIPHER, bot, row) is None

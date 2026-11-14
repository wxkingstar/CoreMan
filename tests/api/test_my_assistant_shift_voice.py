"""「我的助理」页上的即时动作：上班、下班、恢复按时段；重新生成口吻页（每天至多 3 次）。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import cold_start, keys, notes, settings, voice
from coreman.core.assistants.kinds import COLD_START_KIND
from coreman.core.db.models import Bot, FeishuPersonalGrant, User
from coreman.core.feishu_personal import ratelimit
from coreman.core.feishu_personal.service import PersonalError
from coreman.core.timeutils import utcnow
from tests.api.conftest import login_existing
from tests.api.my_assistant_helpers import URL, as_bot_token, code, fresh
from tests.integration.assistant_helpers import SECRET
from tests.integration.job_helpers import owner_with_key, tasks_of

pytestmark = pytest.mark.usefixtures("assistant_master")
SHIFT, VOICE = f"{URL}/shift", f"{URL}/voice/regenerate"
SAMPLES = [f"收到，我下午看一下 {n}" for n in range(6)]
READER: Any = object()


async def _owner(
    client: httpx.AsyncClient, db_session: AsyncSession, *, cold_start_done: bool = True
) -> tuple[Bot, User]:
    bot, owner = await owner_with_key(db_session)
    prefs = await settings.get(db_session, bot.id)
    if cold_start_done:
        prefs.cold_start_started_at = prefs.cold_start_done_at = utcnow()
        prefs.cold_start_outcome = "completed"
    await db_session.commit()
    await login_existing(client, db_session, owner)
    return bot, owner


class Feishu:
    """读飞书的替身：记下被调了几次，可以改成没授权、读不了或样本太少。"""

    def __init__(self) -> None:
        self.samples: list[str] = list(SAMPLES)
        self.authorized = True
        self.error: Exception | None = None
        self.calls = 0
        # 读到一半停住：`entered` 置位表示已经在读，`release` 置位才读完。
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.hold = False


@pytest.fixture
def feishu(monkeypatch: pytest.MonkeyPatch) -> Feishu:
    fake = Feishu()

    async def reader(session: Any, cipher: Any, bot: Any, row: Any) -> Any:
        return READER if fake.authorized else None

    async def collect(
        call: Any, prefs: Any, now: datetime, *, ours: Any, between: Any = None
    ) -> list[str]:
        assert call is READER and await ours(["om_x"]) == frozenset()
        fake.calls += 1
        if between is not None:
            await between()
        if fake.hold:
            fake.entered.set()
            await fake.release.wait()
        if fake.error is not None:
            raise fake.error
        return list(fake.samples)

    monkeypatch.setattr(cold_start, "_reader", reader)
    monkeypatch.setattr(cold_start.voice, "collect", collect)
    return fake


async def test_the_owner_clocks_in_out_and_back_to_the_schedule(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner_row = await _owner(client, db_session)
    assert (await client.patch(f"{URL}/settings", json={"always_on": False})).status_code == 200
    on = await client.post(SHIFT, json={"state": "on"})
    assert on.status_code == 200 and on.headers["cache-control"] == "no-store"
    data = on.json()["data"]
    assert data["shift_override"] == "on" and data["on_shift"] is True
    assert data["shift_override_until"] is not None
    await fresh(db_session, bot)
    prefs = await settings.get(db_session, bot.id)
    # 与私聊里说「上班」同一个效果：记下今天上过班（下班整理凭它触发）。
    assert prefs.shift_override == "on" and prefs.worked_on == settings.local_today(prefs, utcnow())
    seen = (await client.get(URL)).json()["data"]
    assert seen["on_shift"] is True and seen["shift_override"] == "on"
    assert seen["shift_override_until"] == data["shift_override_until"]

    off = (await client.post(SHIFT, json={"state": "off"})).json()["data"]
    assert off["shift_override"] == "off" and off["on_shift"] is False
    assert (await client.get(URL)).json()["data"]["on_shift"] is False

    auto = (await client.post(SHIFT, json={"state": "auto"})).json()["data"]
    assert auto["shift_override"] is None and auto["shift_override_until"] is None
    assert (await client.get(URL)).json()["data"]["shift_override"] is None
    await fresh(db_session, bot)
    assert (await settings.get(db_session, bot.id)).shift_override is None


async def test_around_the_clock_clocking_in_only_cancels_clocking_out(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """全天候（默认）时「下班」照样到下一个上班时刻；「上班」只是取消「下班」，没有到期时刻。"""
    bot, _owner_row = await _owner(client, db_session)
    seen = (await client.get(URL)).json()["data"]
    assert seen["always_on"] is True and seen["shift_override"] is None
    off = (await client.post(SHIFT, json={"state": "off"})).json()["data"]
    assert off["shift_override"] == "off" and off["on_shift"] is False
    assert off["shift_override_until"] is not None
    on = (await client.post(SHIFT, json={"state": "on"})).json()["data"]
    assert on["shift_override"] is None and on["shift_override_until"] is None
    assert on["on_shift"] is True
    await fresh(db_session, bot)
    prefs = await settings.get(db_session, bot.id)
    assert prefs.shift_override is None
    assert prefs.worked_on == settings.local_today(prefs, utcnow())


@pytest.mark.parametrize(
    "body", [{}, {"state": "maybe"}, {"state": "on", "until": "x"}, {"state": 1}]
)
async def test_a_bad_shift_request_changes_nothing(
    client: httpx.AsyncClient, db_session: AsyncSession, body: dict[str, Any]
) -> None:
    bot, _owner_row = await _owner(client, db_session)
    assert (await client.post(SHIFT, json=body)).status_code == 422
    await fresh(db_session, bot)
    assert (await settings.get(db_session, bot.id)).shift_override is None


async def test_shift_needs_csrf_an_interactive_login_and_an_active_assistant(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, owner = await _owner(client, db_session)
    csrf = client.headers.pop("X-CSRF-Token")
    assert (await client.post(SHIFT, json={"state": "on"})).status_code == 403
    assert (await client.post(VOICE)).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    bot.assistant_state = "deprovisioning"
    await db_session.commit()
    for response in (await client.post(SHIFT, json={"state": "on"}), await client.post(VOICE)):
        assert response.status_code == 409 and code(response) == "assistant_not_active"
    as_bot_token(client, monkeypatch, owner)
    assert (await client.post(SHIFT, json={"state": "on"})).status_code == 403
    assert (await client.post(VOICE)).status_code == 403
    await fresh(db_session, bot)
    assert (await settings.get(db_session, bot.id)).shift_override is None


async def test_the_owner_asks_for_a_new_voice_page_at_most_three_times_a_day(
    client: httpx.AsyncClient, db_session: AsyncSession, feishu: Feishu
) -> None:
    bot, _owner_row = await _owner(client, db_session)
    for left in (2, 1, 0):
        response = await client.post(VOICE)
        assert response.status_code == 200, response.text
        assert response.json()["data"] == {"queued": True, "left_today": left}
    queued = await tasks_of(db_session, bot, COLD_START_KIND)
    assert len(queued) == 3 and len({task.dedupe_key for task in queued}) == 3
    refused = await client.post(VOICE)
    assert refused.status_code == 429 and code(refused) == "voice_daily_limit"
    # 到了上限就不再读飞书。
    assert feishu.calls == 3
    assert len(await tasks_of(db_session, bot, COLD_START_KIND)) == 3
    # 样本只在任务的载荷里（按所有者的密钥封存），响应里没有。
    assert "收到" not in response.text


async def test_before_the_cold_start_is_over_it_is_refused(
    client: httpx.AsyncClient, db_session: AsyncSession, feishu: Feishu
) -> None:
    bot, _owner_row = await _owner(client, db_session, cold_start_done=False)
    refused = await client.post(VOICE)
    assert refused.status_code == 409 and code(refused) == "cold_start_unfinished"
    prefs = await settings.get(db_session, bot.id)
    prefs.cold_start_started_at = utcnow()
    await db_session.commit()
    refused = await client.post(VOICE)
    assert refused.status_code == 409 and code(refused) == "cold_start_unfinished"
    assert feishu.calls == 0 and await tasks_of(db_session, bot, COLD_START_KIND) == []


@pytest.mark.parametrize(
    ("setup", "status", "reason"),
    [
        ("unauthorized", 409, "authorization_required"),
        ("feishu_down", 502, "feishu_unavailable"),
        ("few_samples", 409, "too_few_samples"),
        ("edited", 409, "voice_page_edited"),
        ("locked", 409, "content_locked"),
    ],
)
async def test_what_stops_a_new_voice_page(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    feishu: Feishu,
    setup: str,
    status: int,
    reason: str,
) -> None:
    bot, owner = await _owner(client, db_session)
    if setup == "unauthorized":
        feishu.authorized = False
    elif setup == "feishu_down":
        feishu.error = PersonalError("feishu_error")
    elif setup == "few_samples":
        feishu.samples = SAMPLES[: voice.MIN_MESSAGES - 1]
    elif setup == "edited":
        # 所有者自己改过口吻页：那是他本人的话，重新生成不会盖掉它，所以不排这一轮。
        await notes.write(
            db_session, bot.id, voice.PATH, body="我说话很短。", summary="", updated_by="owner"
        )
        await db_session.commit()
    else:
        await keys.destroy_key(db_session, owner.id)
        await db_session.commit()
    refused = await client.post(VOICE)
    assert refused.status_code == status, refused.text
    assert code(refused) == reason and SECRET not in refused.text
    assert await tasks_of(db_session, bot, COLD_START_KIND) == []
    # 没排上的不占当天的次数。
    if setup in ("unauthorized", "feishu_down", "few_samples"):
        feishu.authorized, feishu.error, feishu.samples = True, None, list(SAMPLES)
        assert (await client.post(VOICE)).json()["data"] == {"queued": True, "left_today": 2}


async def test_a_voice_page_the_assistant_wrote_can_be_regenerated(
    client: httpx.AsyncClient, db_session: AsyncSession, feishu: Feishu
) -> None:
    bot, _owner_row = await _owner(client, db_session)
    await voice.save(db_session, bot.id, "他说话很短。")
    await db_session.commit()
    assert (await client.post(VOICE)).status_code == 200
    assert len(await tasks_of(db_session, bot, COLD_START_KIND)) == 1


async def test_an_admin_with_their_own_assistant_only_touches_their_own(
    client: httpx.AsyncClient, db_session: AsyncSession, feishu: Feishu
) -> None:
    bot, _owner_row = await _owner(client, db_session)
    theirs, admin = await owner_with_key(
        db_session, chat_id="oc_admin", platform_user_id="admin_pid", open_id="ou_admin"
    )
    admin.role = "platform_admin"
    prefs = await settings.get(db_session, theirs.id)
    prefs.cold_start_started_at = prefs.cold_start_done_at = utcnow()
    await db_session.commit()
    await login_existing(client, db_session, admin)
    assert (await client.post(SHIFT, json={"state": "off"})).status_code == 200
    assert (await client.post(VOICE)).status_code == 200
    await fresh(db_session, bot, theirs)
    assert (await settings.get(db_session, bot.id)).shift_override is None
    assert (await settings.get(db_session, theirs.id)).shift_override == "off"
    assert await tasks_of(db_session, bot, COLD_START_KIND) == []
    assert len(await tasks_of(db_session, theirs, COLD_START_KIND)) == 1


async def _attempts(db_session: AsyncSession, bot: Bot) -> list[int]:
    found = await db_session.scalar(
        text("SELECT voice_attempts FROM assistant_settings WHERE bot_id = :id"), {"id": bot.id}
    )
    await db_session.commit()
    return list(found or [])


async def test_attempts_that_queue_nothing_are_limited_too(
    client: httpx.AsyncClient, db_session: AsyncSession, feishu: Feishu
) -> None:
    bot, _owner_row = await _owner(client, db_session)
    # 样本太少：每次都读了飞书、却一轮都没排上。
    feishu.samples = SAMPLES[: voice.MIN_MESSAGES - 1]
    for _ in range(cold_start.REGENERATE_HOURLY):
        assert code(await client.post(VOICE)) == "too_few_samples"
    refused = await client.post(VOICE)
    assert refused.status_code == 429 and code(refused) == "voice_attempt_limit"
    assert feishu.calls == cold_start.REGENERATE_HOURLY
    # 一小时之后又能试，直到一天的次数用完。
    stamps = await _attempts(db_session, bot)
    assert len(stamps) == cold_start.REGENERATE_HOURLY
    await db_session.execute(
        text("UPDATE assistant_settings SET voice_attempts = :v ::jsonb WHERE bot_id = :id"),
        {"v": str([stamp - 2 * 3600 for stamp in stamps]), "id": bot.id},
    )
    await db_session.commit()
    for _ in range(cold_start.REGENERATE_ATTEMPTS_DAILY - cold_start.REGENERATE_HOURLY):
        assert code(await client.post(VOICE)) == "too_few_samples"
    await db_session.execute(
        text(
            "UPDATE assistant_settings SET voice_attempts ="
            " (SELECT jsonb_agg(v::bigint - 7200) FROM jsonb_array_elements_text(voice_attempts) v)"
            " WHERE bot_id = :id"
        ),
        {"id": bot.id},
    )
    await db_session.commit()
    refused = await client.post(VOICE)
    assert refused.status_code == 429 and code(refused) == "voice_attempt_limit"
    assert feishu.calls == cold_start.REGENERATE_ATTEMPTS_DAILY
    # 超过一天的尝试不再算。
    await db_session.execute(
        text("UPDATE assistant_settings SET voice_attempts = :v ::jsonb WHERE bot_id = :id"),
        {"v": str([int(utcnow().timestamp()) - 25 * 3600] * 6), "id": bot.id},
    )
    await db_session.commit()
    assert code(await client.post(VOICE)) == "too_few_samples"
    assert len(await _attempts(db_session, bot)) == 1


async def test_a_second_request_while_one_is_reading_is_told_so(
    client: httpx.AsyncClient, db_session: AsyncSession, feishu: Feishu
) -> None:
    bot, _owner_row = await _owner(client, db_session)
    feishu.hold = True
    first = asyncio.create_task(client.post(VOICE))
    await asyncio.wait_for(feishu.entered.wait(), 10)
    # 读飞书之前已经提交：这次尝试别的连接看得到，设置行也没被这个请求锁着。
    locked = await db_session.scalar(
        text(
            "SELECT jsonb_array_length(voice_attempts) FROM assistant_settings"
            " WHERE bot_id = :id FOR UPDATE NOWAIT"
        ),
        {"id": bot.id},
    )
    await db_session.commit()
    assert locked == 1
    second = await client.post(VOICE)
    assert second.status_code == 409 and code(second) == "regenerate_in_progress"
    assert feishu.calls == 1
    feishu.release.set()
    done = await first
    assert done.status_code == 200 and done.json()["data"] == {"queued": True, "left_today": 2}
    # 读完之后标记清掉了：可以再来。
    feishu.hold = False
    assert (await client.post(VOICE)).status_code == 200
    assert len(await tasks_of(db_session, bot, COLD_START_KIND)) == 2


async def test_a_reading_mark_left_by_a_crash_does_not_block_for_long(
    client: httpx.AsyncClient, db_session: AsyncSession, feishu: Feishu
) -> None:
    bot, _owner_row = await _owner(client, db_session)
    prefs = await settings.get(db_session, bot.id)
    prefs.voice_regenerating_at = utcnow() - timedelta(seconds=30)
    await db_session.commit()
    assert code(await client.post(VOICE)) == "regenerate_in_progress"
    prefs.voice_regenerating_at = utcnow() - cold_start.REGENERATE_STALE - timedelta(seconds=1)
    await db_session.commit()
    assert (await client.post(VOICE)).status_code == 200


async def test_a_failed_read_clears_the_mark_and_keeps_the_attempt(
    client: httpx.AsyncClient, db_session: AsyncSession, feishu: Feishu
) -> None:
    bot, _owner_row = await _owner(client, db_session)
    feishu.error = RuntimeError("boom")
    refused = await client.post(VOICE)
    # 读飞书时意外出错不冒成 500（这一次里可能刚换过令牌，事务要照常提交）。
    assert refused.status_code == 502 and code(refused) == "feishu_unavailable"
    await fresh(db_session, bot)
    prefs = await settings.get(db_session, bot.id)
    assert prefs.voice_regenerating_at is None and len(prefs.voice_attempts) == 1


async def test_a_tight_feishu_budget_turns_the_request_away_before_reading(
    client: httpx.AsyncClient, db_session: AsyncSession, feishu: Feishu
) -> None:
    bot, owner = await _owner(client, db_session)
    db_session.add(
        FeishuPersonalGrant(
            bot_id=bot.id,
            user_id=owner.id,
            app_id="cli_example",
            app_fingerprint="f",
            platform_user_id="owner_pid",
            open_id="ou_owner",
            tenant_key="tenant-example",
            status="connected",
        )
    )
    await ratelimit.Budget("cli_example").record(
        db_session,
        ratelimit.HISTORY,
        units=int(ratelimit.LIMITS[ratelimit.HISTORY] * ratelimit.TIGHT),
    )
    await db_session.commit()
    refused = await client.post(VOICE)
    assert refused.status_code == 429 and code(refused) == "feishu_busy"
    # 没读飞书，也不占尝试次数。
    assert feishu.calls == 0 and await _attempts(db_session, bot) == []
    await db_session.execute(text("DELETE FROM feishu_rate_usage"))
    await db_session.commit()
    assert (await client.post(VOICE)).status_code == 200

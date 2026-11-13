"""「我的助理」页的设置：所有者读写工作时段、时区、重点群、忽略的会话与每日预算；每条规则都拦得住。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from coreman.core.assistants import keys, settings
from coreman.core.db.models import Setting
from tests.api.my_assistant_helpers import URL, as_bot_token, code, fresh, owned
from tests.integration.assistant_helpers import raw

pytestmark = pytest.mark.usefixtures("assistant_master")
PREFS = f"{URL}/settings"
STORED = (
    "SELECT coalesce(focus_chats_enc::text, chr(32)) || coalesce(ignored_chats_enc::text, chr(32))"
    " FROM assistant_settings WHERE bot_id = :bot"
)


async def test_the_owner_reads_the_defaults(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    _bot, owner = await owned(client, db_session)
    with capture_logs() as logs:
        response = await client.get(PREFS)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    # 这两项随钟点变。
    assert isinstance(data.pop("on_shift"), bool) and len(data.pop("today")) == 10
    assert data == {
        "timezone": "Asia/Shanghai",
        "work_start": "09:30",
        "work_end": "19:00",
        "workdays": [1, 2, 3, 4, 5],
        "focus_chats": [],
        "ignored_chats": [],
        "shift_override": None,
        "shift_override_until": None,
        "cold_start": "pending",
        "daily_turn_budget": None,
        "platform_daily_turn_budget": 200,
        "effective_daily_turn_budget": 200,
        "limits": {"focus_chats": 5, "ignored_chats": 50, "budget_min": 10},
        "locked": False,
    }
    assert [
        (entry["action"], entry["actor_id"])
        for entry in logs
        if entry["event"] == "assistant_content_access"
    ] == [("settings.read", str(owner.id))]


async def test_the_owner_changes_everything_and_chat_lists_are_stored_encrypted(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    response = await client.patch(
        PREFS,
        json={
            "timezone": "America/New_York",
            "work_start": "08:00",
            "work_end": "17:30",
            "workdays": [6, 1, 2],
            "focus_chats": ["oc_focus_1", "oc_focus_1"],
            "ignored_chats": ["oc_noise_1"],
            "daily_turn_budget": 120,
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["timezone"] == "America/New_York" and data["workdays"] == [1, 2, 6]
    assert (data["work_start"], data["work_end"]) == ("08:00", "17:30")
    assert data["focus_chats"] == ["oc_focus_1"] and data["ignored_chats"] == ["oc_noise_1"]
    assert data["daily_turn_budget"] == 120
    stored = await raw(db_session, STORED, bot=bot.id)
    assert "oc_focus_1" not in stored and "oc_noise_1" not in stored
    # 读回来与写进去的一致；只改一项时别的不动。
    assert (await client.get(PREFS)).json()["data"]["focus_chats"] == ["oc_focus_1"]
    cleared = await client.patch(PREFS, json={"daily_turn_budget": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["data"]["daily_turn_budget"] is None
    assert cleared.json()["data"]["ignored_chats"] == ["oc_noise_1"]
    emptied = await client.patch(PREFS, json={"ignored_chats": []})
    assert emptied.json()["data"]["ignored_chats"] == []
    await fresh(db_session, bot)
    row = await settings.get(db_session, bot.id)
    assert row.timezone == "America/New_York" and row.daily_turn_budget is None


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({}, "nothing_to_update"),
        # 前六项给 null 等同于没给。
        ({"timezone": None}, "nothing_to_update"),
        ({"timezone": "Asia/Nowhere"}, "invalid_timezone"),
        ({"timezone": ""}, "invalid_timezone"),
        ({"work_start": "9:30"}, "invalid_work_hours"),
        ({"work_end": "24:00"}, "invalid_work_hours"),
        # 只改一头，和库里的另一头合起来看：默认 09:30–19:00。
        ({"work_end": "09:00"}, "invalid_work_hours"),
        ({"work_start": "19:00"}, "invalid_work_hours"),
        ({"work_start": "20:00", "work_end": "08:00"}, "invalid_work_hours"),
        ({"workdays": []}, "invalid_workdays"),
        ({"workdays": [8]}, "invalid_workdays"),
        ({"workdays": [1, 1]}, "invalid_workdays"),
        ({"focus_chats": ["ou_person"]}, "invalid_chats"),
        ({"focus_chats": [f"oc_{n}" for n in range(6)]}, "too_many_chats"),
        ({"ignored_chats": [f"oc_{n}" for n in range(51)]}, "too_many_chats"),
        ({"focus_chats": ["oc_a"], "ignored_chats": ["oc_a"]}, "chat_in_both_lists"),
        ({"daily_turn_budget": 201}, "budget_over_platform"),
        # 一项不过，整次请求都不落库（时区没有被改掉，见下面的断言）。
        ({"timezone": "UTC", "daily_turn_budget": 201}, "budget_over_platform"),
        # 类型不对、范围离谱、不认识的键：由请求校验挡下，原因是校验器给的类型名。
        ({"workdays": [True]}, "int_type"),
        ({"workdays": ["1"]}, "int_type"),
        ({"focus_chats": "oc_a"}, "list_type"),
        ({"daily_turn_budget": 5}, "greater_than_equal"),
        ({"daily_turn_budget": 2001}, "less_than_equal"),
        ({"daily_turn_budget": "200"}, "int_type"),
        ({"chat_id": "oc_other"}, "extra_forbidden"),
        ({"cold_start_done_at": "2026-11-13T00:00:00Z"}, "extra_forbidden"),
        ({"shift_override": "on"}, "extra_forbidden"),
    ],
)
async def test_every_rule_is_enforced(
    client: httpx.AsyncClient, db_session: AsyncSession, body: dict[str, Any], reason: str | None
) -> None:
    bot, _owner = await owned(client, db_session)
    response = await client.patch(PREFS, json=body)
    assert response.status_code == 422, response.text
    assert code(response) == reason
    await fresh(db_session, bot)
    row = await settings.get(db_session, bot.id)
    assert (row.timezone, row.work_start, row.work_end) == ("Asia/Shanghai", "09:30", "19:00")
    assert list(row.workdays) == [1, 2, 3, 4, 5]
    assert row.daily_turn_budget is None and not row.focus_chats_enc


async def test_a_chat_cannot_end_up_in_both_lists_across_two_requests(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await owned(client, db_session)
    assert (await client.patch(PREFS, json={"ignored_chats": ["oc_a"]})).status_code == 200
    refused = await client.patch(PREFS, json={"focus_chats": ["oc_a"]})
    assert refused.status_code == 422 and code(refused) == "chat_in_both_lists"


async def test_the_budget_cannot_exceed_the_platform_default(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await owned(client, db_session)
    db_session.add(Setting(key="assistant_daily_turn_budget", value=80))
    await db_session.commit()
    assert (await client.get(PREFS)).json()["data"]["platform_daily_turn_budget"] == 80
    refused = await client.patch(PREFS, json={"daily_turn_budget": 81})
    assert refused.status_code == 422 and code(refused) == "budget_over_platform"
    accepted = await client.patch(PREFS, json={"daily_turn_budget": 80})
    assert accepted.status_code == 200 and accepted.json()["data"]["daily_turn_budget"] == 80


async def test_writes_need_csrf_an_interactive_login_and_an_active_assistant(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, owner = await owned(client, db_session)
    csrf = client.headers.pop("X-CSRF-Token")
    assert (await client.patch(PREFS, json={"timezone": "UTC"})).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    bot.assistant_state = "provisioning"
    await db_session.commit()
    assert (await client.get(PREFS)).status_code == 200
    refused = await client.patch(PREFS, json={"timezone": "UTC"})
    assert refused.status_code == 409 and code(refused) == "assistant_not_active"
    as_bot_token(client, monkeypatch, owner)
    assert (await client.get(PREFS)).status_code == 403
    assert (await client.patch(PREFS, json={"timezone": "UTC"})).status_code == 403
    await fresh(db_session, bot)
    assert (await settings.get(db_session, bot.id)).timezone == "Asia/Shanghai"


async def test_an_admin_with_their_own_assistant_changes_only_their_own(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    assert (await client.patch(PREFS, json={"focus_chats": ["oc_owner_focus"]})).status_code == 200
    theirs, _admin = await owned(client, db_session, role="platform_admin")
    seen = await client.get(PREFS)
    assert seen.json()["data"]["focus_chats"] == [] and "oc_owner_focus" not in seen.text
    assert (await client.patch(PREFS, json={"timezone": "UTC"})).status_code == 200
    await fresh(db_session, bot, theirs)
    assert (await settings.get(db_session, bot.id)).timezone == "Asia/Shanghai"
    assert (await settings.get(db_session, theirs.id)).timezone == "UTC"


async def test_after_the_key_is_destroyed_chat_lists_cannot_be_written(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    _bot, owner = await owned(client, db_session)
    assert (await client.patch(PREFS, json={"focus_chats": ["oc_focus_1"]})).status_code == 200
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    seen = await client.get(PREFS)
    assert seen.json()["data"]["focus_chats"] == [] and seen.json()["data"]["locked"] is True
    assert "oc_focus_1" not in seen.text
    refused = await client.patch(PREFS, json={"ignored_chats": ["oc_noise_1"]})
    assert refused.status_code == 409 and code(refused) == "content_locked"
    # 不涉及密文的项照常能改。
    assert (await client.patch(PREFS, json={"work_end": "18:00"})).status_code == 200
    assert (await client.patch(PREFS, json={"daily_turn_budget": 50})).status_code == 200


async def test_a_lowered_platform_default_shows_as_the_effective_budget(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """管理员把平台默认调到个人值以下：个人值留着，生效的是较小的那个，页面两个都看得到。"""
    await owned(client, db_session)
    assert (await client.patch(PREFS, json={"daily_turn_budget": 150})).status_code == 200
    db_session.add(Setting(key="assistant_daily_turn_budget", value=80))
    await db_session.commit()
    data = (await client.get(PREFS)).json()["data"]
    assert data["daily_turn_budget"] == 150 and data["platform_daily_turn_budget"] == 80
    assert data["effective_daily_turn_budget"] == 80
    assert (await client.get(URL)).json()["data"]["turns"]["budget"] == 80

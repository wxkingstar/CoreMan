"""「我的助理」概览：空状态与开通指引；所有者看到状态、运行时、在班、轮次与轮询健康。"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import keys, settings
from coreman.core.db.models import AssistantWatch, Bot, ChatLog, PlatformApp, Setting
from coreman.core.timeutils import utcnow
from tests.api.conftest import login_as
from tests.api.my_assistant_helpers import URL, as_bot_token, code, owned
from tests.fakes.runtime_node import add_node_relay
from tests.integration.assistant_helpers import SECRET

pytestmark = pytest.mark.usefixtures("assistant_master")
SEEN = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)


def _turn(bot: Bot) -> ChatLog:
    return ChatLog(
        bot_id=bot.id,
        bot_key=bot.bot_key,
        platform="feishu",
        chat_type="single",
        message_type="text",
        status="success",
        request_at=utcnow(),
    )


async def test_without_an_assistant_the_page_says_how_to_get_one(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    db_session.add(
        PlatformApp(
            platform="feishu",
            name="个人助理",
            capabilities=["assistant"],
            app_id="cli_example",
            secret_enc="x",
        )
    )
    await login_as(client, db_session, role="member")
    response = await client.get(URL)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["data"] == {
        "exists": False,
        "eligible": True,
        "reason": None,
        "how_to": {
            "app_name": "个人助理",
            "steps": ["open_private_chat", "accept_offer", "authorize"],
        },
        "lifecycle": None,
    }


async def test_the_empty_state_says_when_the_member_is_out_of_scope(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    db_session.add(
        Setting(
            key="assistant_eligibility",
            value={"mode": "listed", "department_ids": [], "user_ids": []},
        )
    )
    await login_as(client, db_session, role="member")
    data = (await client.get(URL)).json()["data"]
    assert data["exists"] is False and data["eligible"] is False
    assert data["reason"] == "not_listed" and data["how_to"]["app_name"] is None


async def test_the_owner_sees_state_runtime_shift_turns_and_watch(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    relay = await add_node_relay(db_session, name="pool-1")
    bot.relay_server_id = relay.id
    bot.description = SECRET
    prefs = await settings.get(db_session, bot.id)
    prefs.shift_override, prefs.daily_turn_budget = "on", 50
    db_session.add(AssistantWatch(bot_id=bot.id, state="polling", last_ok_at=SEEN))
    db_session.add_all([_turn(bot) for _ in range(3)])
    await db_session.commit()
    response = await client.get(URL)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    assert data["exists"] is True and data["bot_id"] == str(bot.id)
    assert data["name"] == "个人助理" and data["state"] == "active"
    assert data["runtime"]["name"] == "pool-1" and isinstance(data["runtime"]["online"], bool)
    assert data["on_shift"] is True and data["always_on"] is True
    assert data["shift_override"] == "on" and data["shift_override_until"] is None
    assert data["cold_start"] == {"state": "pending", "step": None, "outcome": None}
    assert data["turns"] == {"today": 3, "budget": 50}
    assert data["watch"] == {
        "state": "polling",
        "last_ok_at": SEEN.isoformat(),
        "last_error": None,
    }
    assert data["lifecycle"] is None and data["content_locked"] is False
    assert data["approval_system"] == {"name": None, "url": None}
    assert "paused_reason" in data
    assert SECRET not in response.text and "/w" not in response.text


async def test_the_overview_uses_the_platform_budget_and_names_the_approval_system(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coreman.core.config import reset_settings_cache

    monkeypatch.setenv("TOWER_DISPLAY_NAME", "Example Tower")
    monkeypatch.setenv("TOWER_WEB_URL", "https://tower.example.com")
    reset_settings_cache()
    _bot, owner = await owned(client, db_session)
    db_session.add(Setting(key="assistant_daily_turn_budget", value=80))
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    data = (await client.get(URL)).json()["data"]
    assert data["turns"] == {"today": 0, "budget": 80}
    assert data["runtime"] is None and data["watch"] is None
    assert data["content_locked"] is True
    assert data["approval_system"] == {
        "name": "Example Tower",
        "url": "https://tower.example.com",
    }


async def test_a_link_that_is_not_http_is_not_given(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coreman.core.config import reset_settings_cache

    monkeypatch.setenv("TOWER_WEB_URL", "javascript:alert(1)")
    reset_settings_cache()
    await owned(client, db_session)
    assert (await client.get(URL)).json()["data"]["approval_system"]["url"] is None


async def test_another_member_sees_only_their_own_empty_state(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    await login_as(client, db_session, role="platform_admin")
    response = await client.get(URL)
    assert response.json()["data"]["exists"] is False
    assert str(bot.id) not in response.text


async def test_a_stopped_assistant_is_no_assistant(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _owner = await owned(client, db_session)
    bot.enabled, bot.assistant_state = False, "deprovisioned"
    await db_session.commit()
    assert (await client.get(URL)).json()["data"]["exists"] is False


async def test_a_bot_token_is_refused(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bot, owner = await owned(client, db_session)
    as_bot_token(client, monkeypatch, owner)
    response = await client.get(URL)
    assert response.status_code == 403 and code(response) is None


async def test_signed_out_is_401(client: httpx.AsyncClient) -> None:
    assert (await client.get(URL)).status_code == 401


async def test_an_assistant_that_is_not_active_is_never_on_shift(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """与管理台列表同一口径：在班要求记录启用且开通好，停用中的助理不显示在班。"""
    bot, _owner = await owned(client, db_session)
    prefs = await settings.get(db_session, bot.id)
    prefs.shift_override = "on"
    await db_session.commit()
    assert (await client.get(URL)).json()["data"]["on_shift"] is True
    bot.assistant_state = "deprovisioning"
    await db_session.commit()
    data = (await client.get(URL)).json()["data"]
    assert data["state"] == "deprovisioning" and data["on_shift"] is False

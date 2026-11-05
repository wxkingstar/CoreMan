"""API 令牌、外部调用与「API 驱动」设置的 HTTP 层：认证、权限、只显示一次的明文、审计。"""

from __future__ import annotations

from sqlalchemy import select

from coreman.core.db.models import (
    AuditLog,
    BotConversationAlias,
    PersonalAccessToken,
    User,
    UserIdentity,
    UserReached,
)
from tests.api.conftest import login_as, login_existing
from tests.integration.worker_helpers import seed_bot


async def _bot(db_session):  # type: ignore[no-untyped-def]
    bot, _, _ = await seed_bot(db_session)
    bot.platform = "feishu"
    await db_session.commit()
    return bot


async def _member_with_token(client, db_session, bot):  # type: ignore[no-untyped-def]
    user = await login_as(client, db_session, login_name="zs", display_name="张三")
    db_session.add(UserIdentity(user_id=user.id, platform="feishu", platform_user_id="zs"))
    db_session.add(UserReached(bot_id=bot.id, user_id=user.id, platform_chat_id="p2p-zs"))
    await db_session.commit()
    options = (await client.get("/api/me/api-token-bots")).json()["data"]
    assert [o["bot_key"] for o in options] == ["sales_bot"] and not options[0]["api_access"]
    created = await client.post(
        "/api/me/api-tokens", json={"name": "脚本", "bot_ids": [str(bot.id)], "days": 30}
    )
    assert created.status_code == 201, created.text
    return user, created.json()["data"]


async def test_token_lifecycle_and_plaintext_shown_once(client, db_session):
    bot = await _bot(db_session)
    _, data = await _member_with_token(client, db_session, bot)
    plaintext = data["token"]
    assert plaintext.startswith("cmt_") and data["prefix"] == plaintext[:12]
    listed = (await client.get("/api/me/api-tokens")).json()["data"]
    assert len(listed) == 1 and "token" not in listed[0] and listed[0]["status"] == "active"
    stored = await db_session.scalar(select(PersonalAccessToken))
    assert plaintext not in (stored.token_hash, stored.token_prefix)
    too_long = await client.post(
        "/api/me/api-tokens", json={"name": "x", "bot_ids": [str(bot.id)], "days": 366}
    )
    assert too_long.status_code == 422
    revoked = await client.delete(f"/api/me/api-tokens/{data['id']}")
    assert revoked.json()["data"]["status"] == "revoked"
    denied = await client.post(
        "/api/infra/tasks",
        json={"bot_key": "sales_bot", "prompt": "hi"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert denied.status_code == 401
    actions = set(await db_session.scalars(select(AuditLog.action)))
    assert {"api_token.create", "api_token.revoke"} <= actions


async def test_external_calls_need_bot_opt_in_and_reject_browsers(client, db_session):
    bot = await _bot(db_session)
    _, data = await _member_with_token(client, db_session, bot)
    auth = {"Authorization": f"Bearer {data['token']}"}
    body = {"bot_key": "sales_bot", "prompt": "整理告警", "request_id": "r-1"}
    off = await client.post("/api/infra/tasks", json=body, headers=auth)
    assert off.status_code == 403 and "允许 API 驱动" in off.json()["message"]
    owner = await db_session.get(User, bot.created_by)
    await login_existing(client, db_session, owner)
    toggled = await client.patch(f"/api/admin/bots/{bot.id}/api-access", json={"enabled": True})
    assert toggled.status_code == 200 and toggled.json()["data"]["enabled"] is True
    browser = await client.post(
        "/api/infra/tasks", json=body, headers={**auth, "Origin": "https://evil.example"}
    )
    assert browser.status_code == 403
    created = await client.post("/api/infra/tasks", json=body, headers=auth)
    assert created.status_code == 202, created.text
    task = created.json()["data"]
    assert task["status"] == "queued" and task["conversation"] == {"target": "p2p"}
    replay = await client.post("/api/infra/tasks", json=body, headers=auth)
    assert replay.status_code == 200 and replay.json()["data"]["id"] == task["id"]
    got = await client.get(f"/api/infra/tasks/{task['id']}", headers=auth)
    assert got.json()["data"]["id"] == task["id"]
    cancelled = await client.post(f"/api/infra/tasks/{task['id']}/cancel", headers=auth)
    assert cancelled.status_code == 200
    assert (await client.get(f"/api/infra/tasks/{task['id']}")).status_code == 401
    actions = set(await db_session.scalars(select(AuditLog.action)))
    assert {"bot.api_access", "api_task.create"} <= actions


async def test_bot_settings_only_for_managers_and_alias_removal(client, db_session):
    bot = await _bot(db_session)
    row = BotConversationAlias(bot_id=bot.id, alias="订单异常", chat_id="oc_g")
    db_session.add(row)
    await db_session.commit()
    await login_as(client, db_session, role="platform_admin")
    assert (await client.get(f"/api/admin/bots/{bot.id}/api-access")).status_code == 403
    await login_existing(client, db_session, await db_session.get(User, bot.created_by))
    data = (await client.get(f"/api/admin/bots/{bot.id}/api-access")).json()["data"]
    assert data["enabled"] is False and [a["alias"] for a in data["aliases"]] == ["订单异常"]
    removed = await client.delete(f"/api/admin/bots/{bot.id}/api-aliases/{row.id}")
    assert removed.status_code == 200 and removed.json()["data"]["aliases"] == []

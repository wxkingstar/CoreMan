"""The only AI-peer setting left is the target's switch: may other employees ask it for help."""

import pytest
from sqlalchemy import select

from coreman.api.routers import feishu_apps as feishu_app_routes
from coreman.core.db.models import AuditLog, BotCollaboration, User
from tests.api.conftest import login_as, login_existing
from tests.integration.test_bot_collaboration import setup


@pytest.fixture
async def configured(client, db_session, monkeypatch):
    a, b, human, route, task, ledger = await setup(db_session)
    owner = await db_session.get(User, b.created_by)
    await login_existing(client, db_session, owner)

    async def granted(cipher, bot):
        return "granted"

    monkeypatch.setattr(feishu_app_routes, "collaboration_permission", granted)
    return a, b, ledger


def endpoint(bot):
    return f"/api/admin/bots/{bot.id}/bot-help"


async def test_reads_switch_permission_and_incoming_requests(client, configured):
    _, b, _ = configured
    response = await client.get(endpoint(b))
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "accept_bot_help": True,
        "permission": "granted",
        "active_count": 1,
    }


async def test_turning_off_cancels_incoming_requests_and_is_audited(client, db_session, configured):
    _, b, ledger = configured
    response = await client.patch(endpoint(b), json={"enabled": False})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["accept_bot_help"] is False
    assert response.json()["data"]["active_count"] == 0
    await db_session.refresh(b)
    await db_session.refresh(ledger)
    assert b.accept_bot_help is False and ledger.status == "cancelled"
    assert await db_session.get(BotCollaboration, ledger.id) is not None
    actions = list(
        await db_session.scalars(
            select(AuditLog.diff).where(AuditLog.action == "bot.collaboration.accept_bot_help")
        )
    )
    assert actions == [{"accept_bot_help": False}]
    response = await client.patch(endpoint(b), json={"enabled": True})
    assert response.json()["data"]["accept_bot_help"] is True


async def test_unknown_permission_when_the_app_cannot_be_inspected(client, configured, monkeypatch):
    _, b, _ = configured

    async def denied(cipher, bot):
        from coreman.api.errors import ApiError

        raise ApiError(409, 409, "应用缺少所需权限")

    monkeypatch.setattr(feishu_app_routes, "collaboration_permission", denied)
    assert (await client.get(endpoint(b))).json()["data"]["permission"] == "unknown"


async def test_only_its_managers_can_read_or_change(client, db_session, configured):
    _, b, _ = configured
    await login_as(client, db_session, role="member")
    assert (await client.get(endpoint(b))).status_code == 403
    assert (await client.patch(endpoint(b), json={"enabled": False})).status_code == 403


async def test_csrf_required(client, configured):
    _, b, _ = configured
    client.headers.pop("X-CSRF-Token")
    assert (await client.patch(endpoint(b), json={"enabled": False})).status_code == 403

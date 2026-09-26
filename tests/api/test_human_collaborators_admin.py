"""Managers of an employee choose which colleagues it may ask; no bot permission is granted."""

import pytest
from sqlalchemy import select

from coreman.core.db.models import AuditLog, BotHumanPartner, HumanCollaboration, User, UserIdentity
from tests.api.conftest import login_as, login_existing
from tests.integration.test_human_collaboration import handoff, register, setup


@pytest.fixture
async def configured(client, db_session, db_engine):
    bot, origin, helper, partner, task = await setup(db_session)
    owner = await db_session.get(User, bot.created_by)
    await login_existing(client, db_session, owner)
    return bot, origin, helper, partner, task


def endpoint(bot, suffix=""):
    return f"/api/admin/bots/{bot.id}/human-collaborators{suffix}"


async def test_list_options_and_only_manager_access(client, db_session, configured):
    bot, origin, helper, partner, _ = configured
    listed = (await client.get(endpoint(bot))).json()["data"]
    assert listed == [
        {
            "id": str(partner.id),
            "user_id": str(helper.id),
            "name": "库存专家",
            "position": "库存主管",
            "department": "",
            "responsibility": "负责库存口径",
            "enabled": True,
            "version": 1,
            "reachable": True,
            "active_count": 0,
        }
    ]
    unbound = User(login_name="nofeishu", display_name="库存外包")
    db_session.add(unbound)
    await db_session.commit()
    options = await client.get(f"/api/admin/bots/{bot.id}/human-collaborator-options?q=库存")
    data = options.json()["data"]
    # Only people the Feishu app can reach are offered; already added ones are marked.
    assert [(p["name"], p["added"]) for p in data] == [("库存专家", True)]
    await login_as(client, db_session, role="platform_admin")
    assert (await client.get(endpoint(bot))).status_code == 403
    assert (await client.post(endpoint(bot), json={"user_id": str(origin.id)})).status_code == 403


async def test_add_edit_pause_and_remove_cancel_waiting_asks(
    client, db_session, db_engine, configured
):
    bot, origin, helper, partner, task = configured
    response = await client.post(
        endpoint(bot), json={"user_id": str(origin.id), "responsibility": " 审批 "}
    )
    assert response.status_code == 201, response.text
    added = response.json()["data"]
    assert added["responsibility"] == "审批" and added["enabled"]
    assert (await client.post(endpoint(bot), json={"user_id": str(origin.id)})).status_code == 409
    row = await register(db_session, task, origin, helper)
    await handoff(db_session, db_engine, task, row)
    listed = {p["id"]: p for p in (await client.get(endpoint(bot))).json()["data"]}
    assert listed[str(partner.id)]["active_count"] == 1
    edited = await client.patch(
        endpoint(bot, f"/{partner.id}"),
        json={"responsibility": "库存与盘点"},
        headers={"If-Match": '"1"'},
    )
    assert edited.status_code == 200 and edited.json()["data"]["version"] == 2
    assert (
        await client.patch(
            endpoint(bot, f"/{partner.id}"), json={"enabled": False}, headers={"If-Match": '"1"'}
        )
    ).status_code == 409
    paused = await client.patch(
        endpoint(bot, f"/{partner.id}"), json={"enabled": False}, headers={"If-Match": '"2"'}
    )
    assert paused.status_code == 200 and not paused.json()["data"]["enabled"]
    await db_session.refresh(row)
    assert row.status == "cancelled"
    removed = await client.delete(endpoint(bot, f"/{partner.id}"), headers={"If-Match": '"3"'})
    assert removed.status_code == 204
    assert str(partner.id) not in {
        p["id"] for p in (await client.get(endpoint(bot))).json()["data"]
    }
    assert await db_session.get(HumanCollaboration, row.id) is not None
    actions = set(await db_session.scalars(select(AuditLog.action)))
    assert {
        "bot.collaboration.human_add",
        "bot.collaboration.human_edit",
        "bot.collaboration.human_pause",
        "bot.collaboration.human_remove",
    } <= actions


async def test_rejects_unreachable_people_and_other_platforms(client, db_session, configured):
    bot, origin, helper, partner, _ = configured
    ghost = User(login_name="ghost", display_name="无飞书")
    db_session.add(ghost)
    await db_session.commit()
    assert (await client.post(endpoint(bot), json={"user_id": str(ghost.id)})).status_code == 422
    db_session.add(UserIdentity(user_id=ghost.id, platform="feishu", platform_user_id="ghost-id"))
    ghost.status = "disabled"
    await db_session.commit()
    assert (await client.post(endpoint(bot), json={"user_id": str(ghost.id)})).status_code == 422
    bot.platform = "wecom"
    await db_session.commit()
    assert (await client.get(endpoint(bot))).status_code == 422
    assert (
        await db_session.scalar(select(BotHumanPartner).where(BotHumanPartner.user_id == ghost.id))
        is None
    )

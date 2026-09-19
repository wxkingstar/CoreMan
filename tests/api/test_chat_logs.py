import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import Bot, BotMember, ChatLog, Team, User
from tests.api.conftest import login_as


async def _seed(db_session: AsyncSession) -> tuple[Bot, Bot, User]:
    team = Team(slug="sales", name_zh="销售")
    other = Team(slug="ops", name_zh="运维")
    db_session.add_all([team, other])
    await db_session.flush()
    creator = User(login_name="creator", display_name="创建者", team_id=team.id)
    stranger = User(login_name="stranger", display_name="路人", team_id=other.id)
    db_session.add_all([creator, stranger])
    await db_session.flush()
    mine = Bot(
        bot_key="mine",
        platform="wecom",
        name="销售助理",
        created_by=creator.id,
        team_id=team.id,
        model="m",
        working_dir="/d",
        credentials_enc="enc:v1:x",
    )
    theirs = Bot(
        bot_key="theirs",
        platform="wecom",
        name="运营助理",
        created_by=stranger.id,
        team_id=other.id,
        model="m",
        working_dir="/d",
        credentials_enc="enc:v1:x",
    )
    db_session.add_all([mine, theirs])
    await db_session.flush()
    now = datetime.now(UTC)
    seeds = [
        (mine, creator.id, "success"),
        (mine, None, "error"),
        (theirs, stranger.id, "success"),
        (theirs, creator.id, "stopped"),
    ]
    for i, (bot, uid, status) in enumerate(seeds):
        db_session.add(
            ChatLog(
                bot_id=bot.id,
                bot_key=bot.bot_key,
                platform="wecom",
                user_id=uid,
                user_login="creator" if uid == creator.id else None,
                chat_type="single",
                chat_id="c",
                session_key="c",
                message_type="text",
                message_content=f"问题{i}" + "x" * 300,
                response_content=f"回答{i}",
                status=status,
                latency_ms=100 * (i + 1),
                input_tokens=10,
                output_tokens=5,
                request_at=now - timedelta(minutes=i),
                tools_used=["Bash"],
            )
        )
    await db_session.commit()
    return mine, theirs, creator


async def test_visibility_by_role(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    mine, theirs, creator = await _seed(db_session)
    from tests.api.conftest import login_existing

    await login_existing(client, db_session, creator)
    data = (await client.get("/api/admin/chat-logs")).json()["data"]
    assert {d["bot_name"] for d in data["items"]} == {"销售助理", "运营助理"}
    # 我的 bot 2 条 + 我参与的 1 条
    assert data["total"] == 3 and {d["bot_key"] for d in data["items"]} == {"mine", "theirs"}
    assert all(
        "response_content" not in d and len(d["message_preview"]) <= 200 for d in data["items"]
    )
    outsider = await login_as(client, db_session, role="member", team_id=None)
    assert (await client.get("/api/admin/chat-logs")).json()["data"]["total"] == 0
    db_session.add(BotMember(bot_id=theirs.id, user_id=outsider.id, added_by=outsider.id))
    await db_session.commit()
    assert (await client.get("/api/admin/chat-logs")).json()["data"]["total"] == 2
    await login_as(client, db_session, role="team_lead", team_id=mine.team_id)
    assert (await client.get("/api/admin/chat-logs")).json()["data"]["total"] == 2
    await login_as(client, db_session, role="ai_committee")
    assert (await client.get("/api/admin/chat-logs")).json()["data"]["total"] == 4
    r = await client.get("/api/admin/chat-logs", params={"status": "error", "bot_id": str(mine.id)})
    assert r.json()["data"]["total"] == 1
    r = await client.get("/api/admin/chat-logs", params={"keyword": "回答3"})
    assert r.json()["data"]["total"] == 1 and r.json()["data"]["items"][0]["status"] == "stopped"
    r = await client.get("/api/admin/chat-logs", params={"user": "crea"})
    assert r.json()["data"]["total"] == 2


async def test_detail_and_stats(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    mine, theirs, creator = await _seed(db_session)
    from tests.api.conftest import login_existing

    await login_existing(client, db_session, creator)
    theirs_log = (
        await db_session.execute(
            select(ChatLog).where(ChatLog.bot_id == theirs.id, ChatLog.status == "success")
        )
    ).scalar_one()
    assert (await client.get(f"/api/admin/chat-logs/{theirs_log.id}")).status_code == 404
    mine_log = (
        await db_session.execute(
            select(ChatLog).where(ChatLog.bot_id == mine.id, ChatLog.status == "success")
        )
    ).scalar_one()
    d = (await client.get(f"/api/admin/chat-logs/{mine_log.id}")).json()["data"]
    assert d["response_content"] == "回答0" and d["message_content"].startswith("问题0")
    assert d["bot_name"] == "销售助理"
    assert d["tools_used"] == ["Bash"]
    stats = (await client.get("/api/admin/chat-logs/stats")).json()["data"]
    assert {r["bot_name"] for r in stats["by_bot"]} == {"销售助理", "运营助理"}
    assert stats["total"] == 3 and stats["by_status"] == {"success": 1, "error": 1, "stopped": 1}
    assert stats["tokens"]["input"] == 30 and stats["avg_latency_ms"] > 0
    assert [b["bot_key"] for b in stats["by_bot"]] == ["mine", "theirs"]
    assert stats["by_bot"][0]["total"] == 2
    r = await client.get("/api/admin/chat-logs/stats", params={"bot_id": str(uuid.uuid4())})
    assert r.json()["data"]["total"] == 0


async def test_feishu_private_content_is_owner_only(client, db_session, monkeypatch):
    from coreman.api import bot_auth
    from tests.api.conftest import login_existing

    mine, _, creator = await _seed(db_session)
    row = (await db_session.scalars(select(ChatLog).where(ChatLog.user_id == creator.id))).first()
    row.platform = "feishu"
    row.chat_type = "single"
    row.message_content = "private-unique-secret"
    await db_session.commit()
    for role in ("platform_admin", "ai_committee", "team_lead"):
        await login_as(client, db_session, role=role, team_id=mine.team_id)
        assert (await client.get(f"/api/admin/chat-logs/{row.id}")).status_code == 404
        data = (
            await client.get("/api/admin/chat-logs", params={"keyword": "private-unique-secret"})
        ).json()["data"]
        assert data["total"] == 0
        stats = (
            await client.get(
                "/api/admin/chat-logs/stats", params={"keyword": "private-unique-secret"}
            )
        ).json()["data"]
        assert stats["total"] == 0
    await login_existing(client, db_session, creator)
    assert (await client.get(f"/api/admin/chat-logs/{row.id}")).status_code == 200

    async def token_user(request, session):
        request.state.bot_token_authenticated = True
        request.state.admin_session = None
        return creator

    monkeypatch.setattr(bot_auth, "token_user", token_user)
    client.cookies.set("bot_token", "verified-owner-token")
    assert (await client.get(f"/api/admin/chat-logs/{row.id}")).status_code == 404
    assert (
        await client.get("/api/admin/chat-logs", params={"keyword": "private-unique-secret"})
    ).json()["data"]["total"] == 0

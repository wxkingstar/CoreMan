from sqlalchemy import func, select

from coreman.api.routers import health_report
from coreman.core.db.models import AuditLog, ChatSession, User, UserIdentity
from coreman.core.relay.sse import FinishEvent, TextDelta
from tests.api.conftest import login_as, login_existing
from tests.integration.worker_helpers import seed_bot


async def prepare(client, session):
    bot, _, _ = await seed_bot(
        session, env={"API_KEY": "synthetic-health-secret", "BOT_USER_LOGIN": "forged"}
    )
    actor = await session.get(User, bot.created_by)
    actor.source = "sync"
    session.add(
        UserIdentity(user_id=actor.id, platform="wecom", platform_user_id="actual-operator")
    )
    await session.commit()
    await login_existing(client, session, actor)
    return bot


async def test_health_uses_operator_no_session_and_redacts_split_credentials(
    client, db_session, monkeypatch
):
    bot = await prepare(client, db_session)
    requests = []
    closed = []

    class Fake:
        async def chat_stream(self, request, **kwargs):
            requests.append(request)
            yield TextDelta("检查正常，密钥 synthetic-health-")
            yield TextDelta("secret 不应展示。")
            yield FinishEvent("stop")

        async def aclose(self):
            closed.append(True)

    monkeypatch.setattr(health_report, "make_client", lambda _: Fake())
    response = await client.post(f"/api/admin/bots/{bot.id}/health-report")
    assert response.status_code == 200, response.text
    assert "event: done" in response.text and "synthetic-health-secret" not in response.text
    assert "[REDACTED]" in response.text and closed == [True]
    sent = requests[0]
    assert sent.session_id == "" and sent.env_vars["COREMAN_USER_LOGIN"] == "creator"
    assert sent.env_vars["AGENT_WEWORK_USER_ID"] == "actual-operator"
    assert "forged" not in sent.system_prompt and sent.env_vars["BOT_USER_LOGIN"] != "forged"
    assert await db_session.scalar(select(func.count()).select_from(ChatSession)) == 0
    audit = list(await db_session.scalars(select(AuditLog)))
    assert "synthetic-health-secret" not in str([row.diff for row in audit])
    await login_as(client, db_session, role="platform_admin")
    assert (await client.post(f"/api/admin/bots/{bot.id}/health-report")).status_code == 403


async def test_health_truncated_stream_is_not_success(client, db_session, monkeypatch):
    bot = await prepare(client, db_session)

    class Fake:
        async def chat_stream(self, request, **kwargs):
            yield TextDelta("partial")

        async def aclose(self):
            pass

    monkeypatch.setattr(health_report, "make_client", lambda _: Fake())
    response = await client.post(f"/api/admin/bots/{bot.id}/health-report")
    assert "event: error" in response.text and "event: done" not in response.text


def test_redactor_overlapping_credentials_keeps_longest_match():
    output = health_report.OutputFilter({"API_KEY": "abcdefgh", "API_TOKEN": "abcdefgh123456"})
    content = (
        output.feed("before abcdefgh") + output.feed("123456 after") + output.feed("", final=True)
    )
    assert content == "before [REDACTED] after"

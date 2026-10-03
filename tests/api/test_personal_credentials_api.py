"""个人凭证接口：本轮令牌、本人网页提交与管理。响应与错误里都不能出现值。"""

import json
from datetime import timedelta

from sqlalchemy import func, select, update

from coreman.core.db.models import CredentialRequest, User
from coreman.core.personal_credentials import policy, store
from coreman.core.timeutils import utcnow
from tests.api.conftest import login_existing
from tests.integration.credential_helpers import BODY, VALUES, cap_for, cron_cap, login_app, owner

URL = "/api/runtime/credentials/requests"


def _bearer(app, cap):
    token = policy.issue_capability(app.state.cipher, cap, ttl_seconds=600)
    return {"Authorization": f"Bearer {token}"}


async def _requested(client, app, db_session, **kw):
    bot, user, task, _ = await owner(db_session, **kw)
    r = await client.post(URL, json=BODY, headers=_bearer(app, cap_for(bot, user, task)))
    assert r.status_code == 202, r.text
    return bot, user, task, r.json()["data"]["request_id"]


async def test_runtime_request_auth_and_results(client, app, db_session):
    bot, user, task, _ = await owner(db_session)
    headers = _bearer(app, cap_for(bot, user, task))
    assert (await client.post(URL, json=BODY)).status_code == 401
    assert (
        await client.post(URL, json=BODY, headers={"Authorization": "Bearer x"})
    ).status_code == 401
    browser = {**headers, "Origin": "https://evil.example"}
    assert (await client.post(URL, json=BODY, headers=browser)).status_code == 403
    bad = {"fields": [{"key": "PATH", "label": "x"}], "purpose": "p"}
    assert (await client.post(URL, json=bad, headers=headers)).status_code == 422
    first = await client.post(URL, json=BODY, headers=headers)
    assert first.status_code == 202 and first.json()["data"]["status"] == "form_sent"
    assert "结束本轮" in first.json()["data"]["message"]
    again = await client.post(URL, json=BODY, headers=headers)
    assert again.status_code == 200 and again.json()["data"]["status"] == "already_pending"


async def test_runtime_request_body_is_capped_and_creates_nothing(client, app, db_session):
    bot, user, task, _ = await owner(db_session)
    headers = {**_bearer(app, cap_for(bot, user, task)), "Content-Type": "application/json"}
    padded = {**BODY, "purpose": "x" * (64 * 1024)}
    r = await client.post(URL, content=json.dumps(padded), headers=headers)
    assert r.status_code == 413
    assert await db_session.scalar(select(func.count()).select_from(CredentialRequest)) == 0
    assert (await client.post(URL, content=b"{not json", headers=headers)).status_code == 422


async def test_runtime_request_message_for_cron_does_not_promise_resume(client, app, db_session):
    bot, user, task, _ = await owner(db_session)
    headers = _bearer(app, cron_cap(bot, user, task))
    r = await client.post(URL, json=BODY, headers=headers)
    assert r.status_code == 202, r.text
    message = r.json()["data"]["message"]
    assert "下一次定时运行" in message and "续接" in message and "自动续接" not in message


async def test_runtime_request_on_wecom_without_login_is_409(client, app, db_session):
    bot, user, task, _ = await owner(db_session, platform="wecom")
    r = await client.post(URL, json=BODY, headers=_bearer(app, cap_for(bot, user, task)))
    assert r.status_code == 409


async def test_web_form_is_owner_only_and_submits_once(client, app, db_session):
    await login_app(db_session, "wecom")
    bot, user, task, rid = await _requested(client, app, db_session, platform="wecom")
    stranger = User(login_name="stranger", display_name="别人", source="sync")
    db_session.add(stranger)
    await db_session.commit()
    await login_existing(client, db_session, stranger)
    assert (await client.get(f"/api/me/credential-requests/{rid}")).status_code == 403
    await login_existing(client, db_session, user)
    got = await client.get(f"/api/me/credential-requests/{rid}")
    assert got.status_code == 200 and got.headers["cache-control"] == "no-store"
    data = got.json()["data"]
    assert data["status"] == "open" and [f["key"] for f in data["fields"]] == [
        "DEMO_USERNAME",
        "DEMO_PIN",
    ]
    assert "不会发送给 AI 模型" in data["security_note"]
    csrf = client.headers.pop("X-CSRF-Token")
    assert (
        await client.post(f"/api/me/credential-requests/{rid}/submit", json={"values": VALUES})
    ).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    invalid = await client.post(
        f"/api/me/credential-requests/{rid}/submit", json={"values": {"DEMO_USERNAME": "alice"}}
    )
    assert invalid.status_code == 422 and "alice" not in invalid.text
    saved = await client.post(f"/api/me/credential-requests/{rid}/submit", json={"values": VALUES})
    assert saved.status_code == 200 and saved.json()["data"]["keys"] == [
        "DEMO_PIN",
        "DEMO_USERNAME",
    ]
    assert "pin-778899" not in saved.text
    again = await client.post(f"/api/me/credential-requests/{rid}/submit", json={"values": VALUES})
    assert again.status_code == 409


async def test_expired_web_submit_is_410(client, app, db_session):
    await login_app(db_session, "wecom")
    bot, user, task, rid = await _requested(client, app, db_session, platform="wecom")
    await db_session.execute(
        update(CredentialRequest).values(expires_at=utcnow() - timedelta(minutes=1))
    )
    await db_session.commit()
    await login_existing(client, db_session, user)
    r = await client.post(f"/api/me/credential-requests/{rid}/submit", json={"values": VALUES})
    assert r.status_code == 410
    row = await db_session.scalar(select(CredentialRequest))
    await db_session.refresh(row)
    assert row.status == "expired"


async def test_list_update_delete_own_credentials(client, app, db_session):
    await login_app(db_session, "wecom")
    bot, user, task, rid = await _requested(client, app, db_session, platform="wecom")
    await login_existing(client, db_session, user)
    await client.post(f"/api/me/credential-requests/{rid}/submit", json={"values": VALUES})
    listed = await client.get("/api/me/credentials")
    rows = {r["env_key"]: r for r in listed.json()["data"]}
    assert rows["DEMO_USERNAME"]["value"] == "alice" and rows["DEMO_PIN"]["value"] is None
    assert "pin-778899" not in listed.text
    put = await client.put(f"/api/me/credentials/{bot.id}/DEMO_PIN", json={"value": "pin-222333"})
    assert put.status_code == 200 and "pin-222333" not in put.text
    assert (
        await client.put(f"/api/me/credentials/{bot.id}/MISSING", json={"value": "x"})
    ).status_code == 404
    assert (await client.delete(f"/api/me/credentials/{bot.id}/DEMO_PIN")).status_code == 200
    assert (await client.delete(f"/api/me/credentials/{bot.id}/DEMO_PIN")).status_code == 404


async def test_other_users_credentials_are_isolated(client, app, db_session):
    bot, owner_user, _, cipher = await owner(db_session)
    await store.save(
        db_session,
        cipher,
        bot_id=bot.id,
        user_id=owner_user.id,
        fields=[
            {"key": "DEMO_USERNAME", "label": "账号", "secret": False},
            {"key": "DEMO_PIN", "label": "PIN", "secret": True},
        ],
        values=VALUES,
    )
    await db_session.commit()
    stranger = User(login_name="stranger", display_name="别人", source="sync")
    db_session.add(stranger)
    await db_session.commit()
    await login_existing(client, db_session, stranger)
    listed = await client.get("/api/me/credentials")
    assert listed.status_code == 200 and listed.json()["data"] == []
    assert "alice" not in listed.text and "pin-778899" not in listed.text
    for key in VALUES:
        put = await client.put(f"/api/me/credentials/{bot.id}/{key}", json={"value": "hijacked"})
        assert put.status_code == 404
        assert (await client.delete(f"/api/me/credentials/{bot.id}/{key}")).status_code == 404
    injected = await store.injected(db_session, cipher, bot_id=bot.id, user_id=owner_user.id)
    assert injected.env == VALUES

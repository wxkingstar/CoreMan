"""企业微信个人工具：用本人绑定的授权机器人，只在本人私聊与本人定时任务里按本人选的档位调用。"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from coreman.core.bots.secrets import CREDENTIALS_AAD
from coreman.core.db.models import (
    InboundEvent,
    RelayServer,
    RuntimeNode,
    User,
    UserIdentity,
    WecomPersonalBinding,
)
from coreman.core.wecom_personal import gateway, policy, service, tools
from tests.integration.test_chat_handler import chat_task
from tests.integration.worker_helpers import seed_bot

URL = "/api/runtime/wecom-personal/mcp"
# 对话所在的 AI 员工机器人。
WECOM_BOT = "aib-test-bot"
SECRET = "bot-secret"
# 本人扫码绑定的授权机器人：工具只用它的凭证。
PERSONAL_BOT = "aib-personal-bot"
PERSONAL_SECRET = "personal-secret"
# 智能机器人回调里的发送者常常是企业主体下的密文 userid。
SENDER = "wo-sender-000000000000000000000000"


def context(authorizer=SENDER, bot=PERSONAL_BOT):
    person = f"授权真人用户身份：\n名字：本人  \nID：{authorizer}\n" if authorizer else ""
    return f"<extra_identity_context>\n机器人身份：\n名字：示例\nID：{bot}\n{person}说明\n"


def envelope(result=None, *, error=None):
    inner = {"result": json.dumps(result, ensure_ascii=False)} if result is not None else {}
    if error is not None:
        inner["error"] = error
    return httpx.Response(
        200, json={"errcode": 0, "errmsg": "ok", "results_json": json.dumps(inner)}
    )


def mock_whoami(mock, authorizer=SENDER):
    return mock.post(gateway.BASE_URL + "/identity/whoami").mock(
        return_value=envelope({"extra_identity_context": context(authorizer)})
    )


async def setup(session, app, *, chat_type="single", sender=SENDER):
    bot, _, _ = await seed_bot(session)
    bot.credentials_enc = app.state.cipher.encrypt(
        json.dumps({"bot_id": WECOM_BOT, "secret": SECRET}), CREDENTIALS_AAD
    )
    user = User(login_name="wecom-owner", display_name="Owner", source="sync")
    session.add(user)
    await session.flush()
    session.add(
        UserIdentity(user_id=user.id, platform="wecom", platform_user_id="owner", open_id=SENDER)
    )
    chat_id = sender if chat_type == "single" else "group-1"
    task = await chat_task(
        session, bot, "看看我的待办", sender=sender, chat_type=chat_type, chat_id=chat_id
    )
    event = await session.get(InboundEvent, task.inbound_event_id)
    body = {"msgid": "m1", "aibotid": WECOM_BOT, "chattype": chat_type, "from": {"userid": sender}}
    if chat_type == "group":
        body["chatid"] = chat_id
    event.payload = {
        **event.payload,
        "raw": {"cmd": "aibot_msg_callback", "headers": {"req_id": "r1"}, "body": body},
    }
    await session.commit()
    return bot, user, task


async def supported(session, bot, capabilities=None):
    relay = await session.get(RelayServer, bot.relay_server_id)
    node = await session.get(RuntimeNode, relay.runtime_node_id)
    node.capabilities = capabilities or {"claude": {policy.RUNTIME_CAPABILITY: True}}
    await session.commit()


async def bind(session, app, user, *, level="readonly", authorizer=SENDER, **kw):
    row = WecomPersonalBinding(
        user_id=user.id,
        status="bound",
        enabled=True,
        authorization_level=level,
        wecom_bot_id=PERSONAL_BOT,
        authorizer_id=authorizer,
        authorizer_name="本人",
        bot_name="本人的机器人",
        verified_at=datetime.now(UTC),
        bound_at=datetime.now(UTC),
        bot_fingerprint=service.fingerprint(PERSONAL_BOT, PERSONAL_SECRET),
    )
    session.add(row)
    await session.flush()
    row.credentials_enc = app.state.cipher.encrypt(
        json.dumps({"bot_id": PERSONAL_BOT, "secret": PERSONAL_SECRET}),
        service.aad(row, "credentials_enc"),
    )
    row.token_enc = app.state.cipher.encrypt("tok-1", service.aad(row, "token_enc"))
    for key, item in kw.items():
        setattr(row, key, item)
    await session.commit()
    return row


def rpc(name, arguments=None):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }


def call(method, arguments=None, **extra):
    return rpc("wecom_call", {"method": method, "arguments": arguments or {}, **extra})


def value(response):
    assert response.status_code == 200, response.text
    return json.loads(response.json()["result"]["content"][0]["text"])


async def headers(app, task, user):
    from coreman.core.chat import sessions

    async with app.state.session_factory() as session:
        info = await sessions.get_or_create(
            session,
            bot_id=task.bot_id,
            session_key=task.session_key,
            backend="claude",
            ttl_hours=72,
            speaker_user_id=user.id,
        )
        row = await session.get(WecomPersonalBinding, user.id)
        epoch = row.context_epoch if row else uuid.uuid4()
        await session.commit()
    return {
        "Authorization": "Bearer "
        + policy.issue_capability(
            app.state.cipher,
            task_id=task.id,
            user_id=str(user.id),
            context_epoch=epoch,
            base_session_id=info.relay_session_id,
        )
    }


async def test_missing_capability_is_unauthorized(client):
    assert (await client.post(URL, json=call("todo.list"))).status_code == 401


@pytest.mark.parametrize("mutate", ["group", "feishu", "other_sender", "forged_raw"])
async def test_non_private_origins_are_rejected_before_any_http(client, app, db_session, mutate):
    bot, user, task = await setup(
        db_session, app, chat_type="group" if mutate == "group" else "single"
    )
    await bind(db_session, app, user)
    event = await db_session.get(InboundEvent, task.inbound_event_id)
    if mutate == "feishu":
        bot.platform = "feishu"
    if mutate == "other_sender":
        event.payload = {
            **event.payload,
            "raw": {
                **event.payload["raw"],
                "body": {**event.payload["raw"]["body"], "from": {"userid": "x"}},
            },
        }
    if mutate == "forged_raw":
        event.payload = {**event.payload, "raw": {}}
    await db_session.commit()
    with respx.mock(assert_all_called=False) as mock:
        response = await client.post(
            URL, headers=await headers(app, task, user), json=call("todo.list")
        )
        assert not mock.calls
    assert response.status_code == 403


async def test_without_a_connection_there_are_no_tools(client, app, db_session):
    _, user, task = await setup(db_session, app)
    auth = await headers(app, task, user)
    listed = await client.post(
        URL, headers=auth, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert listed.json()["result"]["tools"] == []
    assert value(await client.post(URL, headers=auth, json=call("todo.list")))["error"] == (
        "authorization_changed"
    )


@pytest.mark.parametrize(
    "level,allowed,denied",
    [
        ("readonly", "todo.list", "todo.create"),
        ("all_except_send", "todo.create", "mail.send"),
        ("all", "mail.send", None),
    ],
)
async def test_tiers_decide_which_methods_are_listed_and_callable(
    client, app, db_session, level, allowed, denied
):
    bot, user, task = await setup(db_session, app)
    await bind(db_session, app, user, level=level)
    auth = await headers(app, task, user)
    listed = await client.post(
        URL, headers=auth, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    tools_by_name = {tool["name"]: tool for tool in listed.json()["result"]["tools"]}
    methods = tools_by_name["wecom_call"]["inputSchema"]["properties"]["method"]["enum"]
    assert allowed in methods and (denied is None or denied not in methods)
    assert not {"message.aibot.send", "chat.messages.list", "disk.files.download"} & set(methods)
    with respx.mock as mock:
        route = mock.post(gateway.BASE_URL + tools.METHODS[allowed].path).mock(
            return_value=envelope({"ok": True})
        )
        out = value(await client.post(URL, headers=auth, json=call(allowed, {"a": 1})))
        assert out == {"ok": True, "content_trust": "external_untrusted_data"}
        payload = json.loads(json.loads(route.calls.last.request.content)["payload"])
        assert payload == {"a": 1}
        assert route.calls.last.request.headers["Authorization"] == "Bearer tok-1"
        if denied:
            out = value(await client.post(URL, headers=auth, json=call(denied)))
            assert out["error"] == "outside_selected_authorization"
        assert len(mock.calls) == 1


async def test_authorizer_is_rechecked_and_a_change_disconnects(client, app, db_session):
    bot, user, task = await setup(db_session, app)
    row = await bind(db_session, app, user, verified_at=datetime.now(UTC) - timedelta(minutes=10))
    auth = await headers(app, task, user)
    with respx.mock as mock:
        mock_whoami(mock)
        mock.post(gateway.BASE_URL + "/todo/list").mock(return_value=envelope({"items": []}))
        assert value(await client.post(URL, headers=auth, json=call("todo.list")))["items"] == []
        await db_session.refresh(row)
        assert row.verified_at > datetime.now(UTC) - timedelta(minutes=1)
    row.verified_at = datetime.now(UTC) - timedelta(minutes=10)
    await db_session.commit()
    with respx.mock as mock:
        mock_whoami(mock, authorizer="wo-someone-else-000000000000000000")
        business = mock.post(gateway.BASE_URL + "/todo/list").mock(return_value=envelope({}))
        out = value(await client.post(URL, headers=auth, json=call("todo.list")))
        assert out["error"] == "authorization_changed" and not business.called
    await db_session.refresh(row)
    assert row.status == "unbound" and row.error == "authorizer_changed"
    assert row.token_enc is None and row.credentials_enc is None and row.authorizer_id is None


async def test_rotated_bot_credentials_force_a_new_token_and_check(client, app, db_session):
    bot, user, task = await setup(db_session, app)
    row = await bind(db_session, app, user, bot_fingerprint="old")
    auth = await headers(app, task, user)
    with respx.mock as mock:
        token = mock.post(gateway.AUTH_URL).mock(
            return_value=httpx.Response(200, json={"errcode": 0, "token": "tok-2"})
        )
        whoami = mock_whoami(mock)
        route = mock.post(gateway.BASE_URL + "/todo/list").mock(return_value=envelope({}))
        value(await client.post(URL, headers=auth, json=call("todo.list")))
        assert token.called and whoami.called
        assert route.calls.last.request.headers["Authorization"] == "Bearer tok-2"
    await db_session.refresh(row)
    assert row.bot_fingerprint == service.fingerprint(PERSONAL_BOT, PERSONAL_SECRET)
    # 换令牌用的是本人授权机器人的凭证，不是对话所在 AI 员工的。
    assert json.loads(token.calls.last.request.content)["bot_id"] == PERSONAL_BOT


@pytest.mark.parametrize("errcode", sorted(gateway.TOKEN_ERRORS))
async def test_an_expired_token_is_replaced_once(client, app, db_session, errcode):
    bot, user, task = await setup(db_session, app)
    row = await bind(db_session, app, user)
    auth = await headers(app, task, user)
    with respx.mock as mock:
        mock.post(gateway.AUTH_URL).mock(
            return_value=httpx.Response(200, json={"errcode": 0, "token": "tok-2"})
        )
        route = mock.post(gateway.BASE_URL + "/todo/list").mock(
            side_effect=[
                httpx.Response(200, json={"errcode": errcode, "errmsg": "expired"}),
                envelope({"items": [1]}),
            ]
        )
        assert value(await client.post(URL, headers=auth, json=call("todo.list")))["items"] == [1]
        assert [c.request.headers["Authorization"] for c in route.calls] == [
            "Bearer tok-1",
            "Bearer tok-2",
        ]
    await db_session.refresh(row)
    assert app.state.cipher.decrypt(row.token_enc, service.aad(row, "token_enc")) == "tok-2"


async def test_business_errors_are_passed_through_without_touching_capabilities(
    client, app, db_session
):
    bot, user, task = await setup(db_session, app)
    row = await bind(db_session, app, user)
    with respx.mock as mock:
        mock.post(gateway.BASE_URL + "/mail/search").mock(
            return_value=envelope(error={"code": 400084, "message": "参数错误"})
        )
        out = value(
            await client.post(URL, headers=await headers(app, task, user), json=call("mail.search"))
        )
    assert out["error"] == "wecom_error" and out["errcode"] == 400084
    assert "capability_state" not in out
    await db_session.refresh(row)
    assert row.capabilities == {}


@pytest.mark.parametrize(
    ("errcode", "state"), [(850002, "unauthorized"), (850003, "expired"), (850001, "invalid")]
)
async def test_capability_errors_are_recorded_with_renewal_guidance(
    client, app, db_session, errcode, state
):
    bot, user, task = await setup(db_session, app)
    row = await bind(db_session, app, user, level="all")
    help_message = "若你是智能机器人创建者，可以[点击这里](https://work.weixin.qq.com/ai/renew)授权"
    with respx.mock as mock:
        mock.post(gateway.BASE_URL + "/mail/send").mock(
            return_value=httpx.Response(
                200, json={"errcode": errcode, "errmsg": "x", "help_message": help_message}
            )
        )
        out = value(
            await client.post(
                URL, headers=await headers(app, task, user), json=call("mail.send", {"a": 1})
            )
        )
    assert out["capability_state"] == state
    assert out["renew_url"] == "https://work.weixin.qq.com/ai/renew"
    assert "本人的机器人" in out["hint"] and "可使用权限" in out["hint"]
    await db_session.refresh(row)
    assert row.capabilities["mail:send"]["state"] == state
    assert row.capabilities["mail:send"]["renew_url"] == "https://work.weixin.qq.com/ai/renew"


async def test_successful_calls_mark_the_capability_usable(client, app, db_session):
    bot, user, task = await setup(db_session, app)
    row = await bind(
        db_session,
        app,
        user,
        level="all_except_send",
        capabilities={
            "doc:write": {"state": "expired", "authorized_at": "2026-01-01T00:00:00+00:00"}
        },
    )
    with respx.mock as mock:
        mock.post(gateway.BASE_URL + "/sheet/contents/update").mock(return_value=envelope({}))
        value(
            await client.post(
                URL,
                headers=await headers(app, task, user),
                json=call("sheet.contents.update", {"docid": "d"}),
            )
        )
    await db_session.refresh(row)
    entry = row.capabilities["doc:write"]
    # 表格与文档共用「文档」授权；从过期变回可用，说明本人刚续期过，从现在重新起算。
    assert entry["state"] == "ok"
    assert datetime.fromisoformat(entry["authorized_at"]) > datetime.now(UTC) - timedelta(minutes=1)


async def test_rejected_credentials_unbind_and_ask_to_rebind(client, app, db_session):
    bot, user, task = await setup(db_session, app)
    row = await bind(db_session, app, user, token_enc=None)
    with respx.mock as mock:
        mock.post(gateway.AUTH_URL).mock(
            return_value=httpx.Response(200, json={"errcode": 853000, "errmsg": "invalid"})
        )
        out = value(
            await client.post(URL, headers=await headers(app, task, user), json=call("todo.list"))
        )
    assert out["error"] == "credentials_rejected" and "重新扫码绑定" in out["hint"]
    await db_session.refresh(row)
    assert row.status == "unbound" and row.error == "credentials_rejected"


async def test_local_files_and_unknown_arguments_are_refused(client, app, db_session):
    bot, user, task = await setup(
        db_session,
        app,
    )
    await bind(db_session, app, user, level="all")
    auth = await headers(app, task, user)
    with respx.mock as mock:
        out = value(
            await client.post(
                URL,
                headers=auth,
                json=call(
                    "mail.send",
                    {"to": {"userids": ["x"]}, "attachments": [{"file_path": "/etc/passwd"}]},
                ),
            )
        )
        assert out["error"] == "local_files_unsupported"
        bad = rpc("wecom_call", {"method": "todo.list", "userid": "someone"})
        assert value(await client.post(URL, headers=auth, json=bad))["error"] == (
            "invalid_tool_or_arguments"
        )
        assert value(await client.post(URL, headers=auth, json=call("todo.nuke")))["error"] == (
            "invalid_tool_or_arguments"
        )
        assert not mock.calls


async def test_long_content_is_paged(client, app, db_session, monkeypatch):
    monkeypatch.setattr(tools, "CONTENT_PAGE", 10)
    bot, user, task = await setup(db_session, app)
    await bind(db_session, app, user)
    auth = await headers(app, task, user)
    with respx.mock as mock:
        mock.post(gateway.BASE_URL + "/doc/contents/get").mock(
            return_value=envelope({"name": "周报", "file_path": "一二三四五六七八九十甲乙丙"})
        )
        first = value(
            await client.post(URL, headers=auth, json=call("doc.contents.get", {"docid": "d"}))
        )
        assert first["content"] == "一二三四五六七八九十" and first["next_content_offset"] == 10
        assert "file_path" not in first
        rest = value(
            await client.post(
                URL, headers=auth, json=call("doc.contents.get", {"docid": "d"}, content_offset=10)
            )
        )
        assert rest["content"] == "甲乙丙" and rest["next_content_offset"] is None


async def test_schema_comes_from_wecom_discovery(client, app, db_session):
    tools._SCHEMAS.clear()
    bot, user, task = await setup(db_session, app)
    await bind(db_session, app, user)
    document = {
        "methods": {
            "list": {"path": "/todo/list", "description": "列表", "request": {"$ref": "Req"}}
        },
        "schemas": {
            "Req": {
                "type": "object",
                "properties": {"deadline": {"$ref": "Deadline"}},
                "x-wecom-hidden": True,
            },
            "Deadline": {"type": "object", "properties": {"value": {"type": "string"}}},
        },
    }
    with respx.mock as mock:
        route = mock.post(gateway.BASE_URL + "/service/discovery").mock(
            return_value=envelope(document)
        )
        out = value(
            await client.post(
                URL,
                headers=await headers(app, task, user),
                json=rpc("wecom_method_schema", {"method": "todo.list"}),
            )
        )
        assert json.loads(json.loads(route.calls.last.request.content)["payload"]) == {
            "service": "todo"
        }
    schema = out["request_schema"]
    assert schema["properties"]["deadline"]["properties"]["value"] == {"type": "string"}
    assert "x-wecom-hidden" not in schema


async def test_reconnecting_invalidates_capabilities_of_the_old_generation(client, app, db_session):
    bot, user, task = await setup(db_session, app)
    row = await bind(db_session, app, user)
    auth = await headers(app, task, user)
    row.context_epoch = uuid.uuid4()
    await db_session.commit()
    with respx.mock as mock:
        assert value(await client.post(URL, headers=auth, json=call("todo.list")))["error"] == (
            "authorization_changed"
        )
        assert not mock.calls


async def test_a_paused_binding_offers_no_tools(client, app, db_session):
    bot, user, task = await setup(db_session, app)
    await bind(db_session, app, user, enabled=False)
    auth = await headers(app, task, user)
    listed = await client.post(
        URL, headers=auth, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert listed.json()["result"]["tools"] == []


async def test_another_member_in_the_same_assistant_never_gets_the_owners_wecom(
    client, app, db_session
):
    """A 绑定了企业微信；B 私聊同一个 AI 员工，拿不到任何企业微信工具，更拿不到 A 的数据。"""
    bot, owner, _ = await setup(db_session, app)
    await bind(db_session, app, owner, level="all")
    other = User(login_name="wecom-other", display_name="Other", source="sync")
    db_session.add(other)
    await db_session.flush()
    other_sender = "wo-other-00000000000000000000000000"
    db_session.add(
        UserIdentity(
            user_id=other.id, platform="wecom", platform_user_id="other", open_id=other_sender
        )
    )
    task = await chat_task(
        db_session,
        bot,
        "看看我的邮件",
        sender=other_sender,
        chat_type="single",
        chat_id=other_sender,
    )
    event = await db_session.get(InboundEvent, task.inbound_event_id)
    body = {
        "msgid": "m2",
        "aibotid": WECOM_BOT,
        "chattype": "single",
        "from": {"userid": other_sender},
    }
    event.payload = {**event.payload, "raw": {"cmd": "aibot_msg_callback", "body": body}}
    await db_session.commit()
    with respx.mock(assert_all_called=False) as mock:
        # B 冒用 A 的身份签发的能力凭据：来源核验只认 B，直接拒绝。
        forged = await headers(app, task, owner)
        assert (await client.post(URL, headers=forged, json=call("mail.search"))).status_code == 403
        own = await headers(app, task, other)
        listed = await client.post(
            URL, headers=own, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        assert listed.json()["result"]["tools"] == []
        assert value(await client.post(URL, headers=own, json=call("mail.search")))["error"] == (
            "authorization_changed"
        )
        assert not mock.calls


async def test_scheduled_capability_revalidates_the_job(app, client, db_session, db_engine):
    from coreman.core.db.models import BotMember, CronJob, UserReached
    from coreman.core.db.session import make_session_factory
    from coreman.runtime.scheduler.cron import run_tick
    from tests.api.test_personal_schedules import claim

    bot, user, _ = await setup(db_session, app)
    row = await bind(db_session, app, user)
    db_session.add(BotMember(bot_id=bot.id, user_id=user.id))
    db_session.add(UserReached(bot_id=bot.id, user_id=user.id, platform_chat_id=SENDER))
    job = CronJob(
        bot_id=bot.id,
        created_by=user.id,
        name="待办汇总",
        cron_expression="* * * * *",
        prompt="汇总我的待办",
        next_run_at=datetime.now(UTC),
    )
    db_session.add(job)
    await db_session.commit()
    await run_tick(make_session_factory(db_engine), job.next_run_at)
    task = await claim(db_session)
    token = policy.issue_capability(
        app.state.cipher,
        task_id=task.id,
        user_id=str(user.id),
        context_epoch=row.context_epoch,
        base_session_id=None,
    )
    auth = {"Authorization": "Bearer " + token}
    with respx.mock as mock:
        mock.post(gateway.BASE_URL + "/todo/list").mock(return_value=envelope({"items": []}))
        assert value(await client.post(URL, headers=auth, json=call("todo.list")))["items"] == []
    # 任务改成发到群里之后，本人的企业微信就不能再用了。
    task.payload = {**task.payload, "config": {**task.payload["config"], "target_chats": ["g"]}}
    await db_session.commit()
    assert (await client.post(URL, headers=auth, json=call("todo.list"))).status_code == 403

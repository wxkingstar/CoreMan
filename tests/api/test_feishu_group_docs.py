"""群里读飞书文档：A 层只读本群看得到的，B 层要提问人逐篇放行。"""

import json
import uuid
from dataclasses import replace

import httpx
import pytest
import respx
from sqlalchemy import select

from coreman.core.bus import tasks
from coreman.core.chat import sessions
from coreman.core.chat.card_replies import REPLY_BUTTON
from coreman.core.db.models import FeishuDocConsent, InboundEvent, OutboxItem, Task
from coreman.core.feishu_docs import consent, links, policy
from coreman.core.feishu_docs.links import LinkError
from coreman.runtime.worker.chat.feishu_docs import configure, remember_links
from tests.api.test_feishu_personal import grant, rpc, setup, value
from tests.api.test_feishu_personal_worker import intake_for
from tests.integration.test_chat_handler import chat_task
from tests.integration.worker_helpers import build_ctx

URL = "/api/runtime/feishu-docs/mcp"
OPEN = "https://open.feishu.cn/open-apis"
DOC = "https://example.feishu.cn/docx/DocToken1234"
OTHER = "https://example.feishu.cn/docx/OtherToken99"
SUPPORTED = {"claude": {"feishu_group_docs_v1": True}}
READ_SCOPES = ["docx:document:readonly", "drive:drive", "docs:permission.member:create"]


def test_links_are_parsed_from_feishu_urls_only():
    link = links.parse("https://example.feishu.cn/base/BaseToken123?table=tblAbc&view=vewX")
    assert (link.kind, link.token, link.table, link.view) == (
        "base",
        "BaseToken123",
        "tblAbc",
        "vewX",
    )
    assert link.url == "https://example.feishu.cn/base/BaseToken123"
    assert links.parse("https://x.larkoffice.com/wiki/WikiNode1234").permission_type == "wiki"
    for bad, code in (
        ("http://example.feishu.cn/docx/DocToken1234", "invalid_link"),
        ("https://feishu.cn.evil.example/docx/DocToken1234", "invalid_link"),
        ("https://example.feishu.cn/docx/../x", "invalid_link"),
        ("https://example.feishu.cn/docs/OldDoc12345", "unsupported_type"),
    ):
        with pytest.raises(LinkError, match=code):
            links.parse(bad)
    text = f"看下 [方案]({DOC}) 和 {OTHER}，还有 {DOC}?from=x"
    assert [item.token for item in links.find(text)] == ["DocToken1234", "OtherToken99"]
    glued = f"结构不合理。\n{DOC}这是新结构，{OTHER}这是业务流程"
    assert [item.token for item in links.find(glued)] == ["DocToken1234", "OtherToken99"]
    base = links.find("见 https://example.feishu.cn/base/BaseToken123?table=tblAbc这张表")[0]
    assert (base.token, base.table) == ("BaseToken123", "tblAbc")


async def group(db_session, app, text=f"帮我总结 {DOC}"):
    bot, user, task = await setup(db_session, app, chat_type="group")
    task.payload = {**task.payload, "doc_links": [link.token for link in links.find(text)]}
    await db_session.commit()
    return bot, user, task


async def headers(app, task):
    async with app.state.session_factory() as session:
        info = await sessions.get_or_create(
            session,
            bot_id=task.bot_id,
            session_key=task.session_key,
            backend="claude",
            ttl_hours=72,
            speaker_user_id=None,
        )
        await session.commit()
    token = policy.issue_capability(
        app.state.cipher,
        task_id=task.id,
        chat_id="oc_group",
        session_id=info.relay_session_id,
        ttl_seconds=600,
    )
    return {"Authorization": "Bearer " + token}


def tenant(mock):
    mock.post(f"{OPEN}/auth/v3/tenant_access_token/internal").mock(
        return_value=httpx.Response(
            200, json={"code": 0, "tenant_access_token": "t-app", "expire": 7200}
        )
    )


def permissions(mock, *, members=(), entity="closed", external=False):
    mock.get(f"{OPEN}/drive/v1/permissions/DocToken1234/members").mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"items": list(members)}})
    )
    mock.get(f"{OPEN}/drive/v1/permissions/DocToken1234/public").mock(
        return_value=httpx.Response(
            200, json={"code": 0, "data": {"permission_public": {"link_share_entity": entity}}}
        )
    )
    mock.get(f"{OPEN}/im/v1/chats/oc_group").mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"external": external}})
    )


def raw_doc(mock):
    return mock.get(f"{OPEN}/docx/v1/documents/DocToken1234/raw_content").mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"content": "群里的方案"}})
    )


async def test_the_app_reads_a_document_shared_with_this_group(client, app, db_session):
    _, _, task = await group(db_session, app)
    with respx.mock as mock:
        tenant(mock)
        permissions(mock, members=[{"member_type": "openchat", "member_id": "oc_group"}])
        route = raw_doc(mock)
        out = value(
            await client.post(
                URL, headers=await headers(app, task), json=rpc("feishu_doc_read", {"url": DOC})
            )
        )
    assert out["content"] == "群里的方案" and out["read_as"] == "app"
    assert out["content_trust"] == "external_untrusted_data"
    assert route.calls[0].request.headers["authorization"] == "Bearer t-app"


@pytest.mark.parametrize(
    ("entity", "external", "readable"),
    [
        ("tenant_readable", False, True),
        ("tenant_readable", True, False),
        ("anyone_readable", True, True),
        ("closed", False, False),
    ],
)
async def test_link_sharing_counts_only_when_this_group_can_open_it(
    client, app, db_session, entity, external, readable
):
    _, _, task = await group(db_session, app)
    with respx.mock as mock:
        tenant(mock)
        # 共享给的是别的群：应用读得到，但本群看不到。
        permissions(
            mock,
            members=[{"member_type": "openchat", "member_id": "oc_finance"}],
            entity=entity,
            external=external,
        )
        route = raw_doc(mock)
        out = value(
            await client.post(
                URL, headers=await headers(app, task), json=rpc("feishu_doc_read", {"url": DOC})
            )
        )
    assert ("content" in out) is readable
    assert route.called is readable
    if not readable:
        assert out["error"] == "no_access" and out["can_request_access"] is False


async def test_unreadable_document_offers_consent_only_to_a_connected_asker(
    client, app, db_session
):
    bot, user, task = await group(db_session, app)
    await grant(
        db_session,
        app,
        bot,
        user,
        authorization_level="all",
        scopes=READ_SCOPES,
        requested_scopes=READ_SCOPES,
    )
    with respx.mock as mock:
        tenant(mock)
        permissions(mock)
        out = value(
            await client.post(
                URL, headers=await headers(app, task), json=rpc("feishu_doc_read", {"url": DOC})
            )
        )
        assert out["can_request_access"] is True
        # 不是提问人这条消息里的链接，不能请求放行。
        other = value(
            await client.post(
                URL,
                headers=await headers(app, task),
                json=rpc("feishu_doc_request_access", {"urls": [OTHER]}),
            )
        )
        assert other["error"] == "link_not_in_message"
        asked = value(
            await client.post(
                URL,
                headers=await headers(app, task),
                json=rpc("feishu_doc_request_access", {"urls": [DOC]}),
            )
        )
        again = value(
            await client.post(
                URL,
                headers=await headers(app, task),
                json=rpc("feishu_doc_request_access", {"urls": [DOC]}),
            )
        )
    assert asked["status"] == "access_requested" and again["status"] == "already_requested"
    row = (await db_session.scalars(select(FeishuDocConsent))).one()
    assert (row.status, row.tokens, row.share_offered) == ("pending", ["DocToken1234"], True)
    item = await db_session.get(OutboxItem, row.card_outbox_id)
    card = json.dumps(item.payload["card"], ensure_ascii=False)
    assert item.target == {"chat_id": "oc_group"}
    assert "<at id=ou_human></at>" in card and DOC in card
    assert '"requester": "human"' in card and consent.SHARE in card


async def click(db_session, bot, origin, label, *, card_message="om_card", sender="human"):
    """提问人在放行卡片上点了按钮：网关把它当作他在本群发的下一句话。"""
    await tasks.finish(db_session, origin.id, status="succeeded")
    await db_session.commit()
    task = await chat_task(
        db_session, bot, label, sender=sender, chat_type="group", chat_id="oc_group"
    )
    event = await db_session.get(InboundEvent, task.inbound_event_id)
    event.sender_open_id = "ou_" + sender
    event.payload = {
        **event.payload,
        "sender": {**event.payload["sender"], "sender_type": "user"},
        "raw": {
            "header": {"event_type": "card.action.trigger", "app_id": "cli_test"},
            "event": {
                "operator": {"open_id": "ou_" + sender, "user_id": sender},
                "context": {"open_chat_id": "oc_group", "open_message_id": card_message},
            },
        },
    }
    event.reply_context = {
        **event.reply_context,
        REPLY_BUTTON: {
            "message_id": card_message,
            "row": consent.ROW,
            "buttons": [],
            "label": label,
            "requester": sender,
        },
    }
    await db_session.commit()
    return task


async def pending_card(db_session, app, bot, user, task):
    await grant(
        db_session,
        app,
        bot,
        user,
        authorization_level="all",
        scopes=READ_SCOPES,
        requested_scopes=READ_SCOPES,
    )
    row = FeishuDocConsent(
        bot_id=bot.id,
        user_id=user.id,
        chat_id="oc_group",
        tokens=["DocToken1234"],
        urls=[DOC],
        status="pending",
        share_offered=True,
        shared=False,
        origin_task_id=task.id,
        expires_at=consent.utcnow() + consent.PENDING_TTL,
    )
    db_session.add(row)
    item = OutboxItem(
        bot_id=bot.id,
        platform="feishu",
        kind="send",
        dedupe_key=f"card-{uuid.uuid4()}",
        target={"chat_id": "oc_group"},
        payload={"card": {}, "_feishu_message_id": "om_card"},
        status="sent",
    )
    db_session.add(item)
    await db_session.flush()
    row.card_outbox_id = item.id
    await db_session.commit()
    return row


async def test_the_askers_click_lets_only_them_read_the_document(
    client, app, db_session, db_engine
):
    bot, user, task = await group(db_session, app)
    row = await pending_card(db_session, app, bot, user, task)
    turn = await click(db_session, bot, task, consent.ONCE)
    intake = await intake_for(db_session, bot, turn, consent.ONCE, SUPPORTED)
    ctx = build_ctx(db_engine, turn)
    ctx.turn_notes = []
    prompt, env = await configure(db_session, ctx, intake, uuid.uuid4(), "你是助理", {})
    await db_session.commit()
    assert "## 群里的飞书文档" in prompt and env[policy.PREFIX + "URL"].endswith(URL)
    assert "不要用浏览器" in prompt
    assert "同意以他的身份读取" in ctx.turn_notes[0]
    await db_session.refresh(row)
    assert row.status == "approved" and row.decided_at is not None
    with respx.mock as mock:
        tenant(mock)
        permissions(mock)
        fetched = mock.post(f"{OPEN}/docs_ai/v1/documents/DocToken1234/fetch").mock(
            return_value=httpx.Response(
                200, json={"code": 0, "data": {"document": {"content": "# 方案"}}}
            )
        )
        out = value(
            await client.post(
                URL, headers=await headers(app, turn), json=rpc("feishu_doc_read", {"url": DOC})
            )
        )
    assert out["content"] == "# 方案" and out["read_as"] == "requester"
    assert fetched.calls[0].request.headers["authorization"] == "Bearer user-secret"
    assert "user-secret" not in json.dumps(out)


async def test_someone_elses_click_or_turn_does_not_use_the_consent(
    client, app, db_session, db_engine
):
    bot, user, task = await group(db_session, app)
    row = await pending_card(db_session, app, bot, user, task)
    turn = await click(db_session, bot, task, consent.ONCE, sender="colleague")
    intake = await intake_for(db_session, bot, turn, consent.ONCE, SUPPORTED)
    ctx = build_ctx(db_engine, turn)
    ctx.turn_notes = []
    await configure(db_session, ctx, intake, uuid.uuid4(), "", {})
    await db_session.commit()
    await db_session.refresh(row)
    assert row.status == "pending" and ctx.turn_notes == []
    # 本人放行之后，别人在本群提问仍然读不到。
    row.status, row.expires_at = "approved", consent.utcnow() + consent.APPROVED_TTL
    await db_session.commit()
    with respx.mock as mock:
        tenant(mock)
        permissions(mock)
        out = value(
            await client.post(
                URL, headers=await headers(app, turn), json=rpc("feishu_doc_read", {"url": DOC})
            )
        )
    assert out["error"] == "no_access"


async def test_share_click_adds_this_group_as_a_reader(app, db_session, db_engine):
    bot, user, task = await group(db_session, app)
    row = await pending_card(db_session, app, bot, user, task)
    turn = await click(db_session, bot, task, consent.SHARE)
    intake = await intake_for(db_session, bot, turn, consent.SHARE, SUPPORTED)
    ctx = build_ctx(db_engine, turn)
    ctx.turn_notes = []
    with respx.mock as mock:
        shared = mock.post(f"{OPEN}/drive/v1/permissions/DocToken1234/members").mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {}})
        )
        await configure(db_session, ctx, intake, uuid.uuid4(), "", {})
    await db_session.commit()
    body = json.loads(shared.calls[0].request.content)
    assert body == {
        "member_type": "openchat",
        "member_id": "oc_group",
        "perm": "view",
        "type": "chat",
    }
    assert shared.calls[0].request.url.params["need_notification"] == "false"
    await db_session.refresh(row)
    assert row.status == "approved" and row.shared is True
    assert "已把本群加为" in ctx.turn_notes[0]


async def test_decline_records_the_refusal(app, db_session, db_engine):
    bot, user, task = await group(db_session, app)
    row = await pending_card(db_session, app, bot, user, task)
    turn = await click(db_session, bot, task, consent.DECLINE)
    intake = await intake_for(db_session, bot, turn, consent.DECLINE, SUPPORTED)
    ctx = build_ctx(db_engine, turn)
    ctx.turn_notes = []
    await configure(db_session, ctx, intake, uuid.uuid4(), "", {})
    await db_session.commit()
    await db_session.refresh(row)
    assert row.status == "declined" and "拒绝" in ctx.turn_notes[0]


@pytest.mark.parametrize("denial", ["private", "observe", "old_runtime", "wecom"])
async def test_no_docs_tools_outside_ordinary_group_turns(app, db_session, db_engine, denial):
    bot, _, task = await setup(
        db_session, app, chat_type="single" if denial == "private" else "group"
    )
    if denial == "observe":
        task.payload = {**task.payload, "origin": "observe"}
    if denial == "wecom":
        bot.platform = "wecom"
    await db_session.commit()
    capabilities = {"claude": {}} if denial == "old_runtime" else SUPPORTED
    intake = await intake_for(db_session, bot, task, "看下文档", capabilities)
    ctx = build_ctx(db_engine, task)
    ctx.turn_notes = []
    initial = {policy.PREFIX + "TOKEN": "forged", "OTHER": "ok"}
    assert await configure(db_session, ctx, intake, uuid.uuid4(), "p", initial) == (
        "p",
        {"OTHER": "ok"},
    )


async def test_capability_stops_after_a_reset_or_for_observe_turns(client, app, db_session):
    _, _, task = await group(db_session, app)
    good = await headers(app, task)
    stale = policy.issue_capability(
        app.state.cipher,
        task_id=task.id,
        chat_id="oc_group",
        session_id=uuid.uuid4(),
        ttl_seconds=600,
    )
    response = await client.post(
        URL, headers={"Authorization": "Bearer " + stale}, json=rpc("feishu_doc_read", {"url": DOC})
    )
    assert response.status_code == 403
    other_chat = policy.issue_capability(
        app.state.cipher,
        task_id=task.id,
        chat_id="oc_other",
        session_id=uuid.uuid4(),
        ttl_seconds=600,
    )
    response = await client.post(
        URL,
        headers={"Authorization": "Bearer " + other_chat},
        json=rpc("feishu_doc_read", {"url": DOC}),
    )
    assert response.status_code == 403
    row = await db_session.get(Task, task.id)
    row.payload = {**row.payload, "origin": "observe"}
    await db_session.commit()
    response = await client.post(URL, headers=good, json=rpc("feishu_doc_read", {"url": DOC}))
    assert response.status_code == 403


async def test_links_in_the_message_are_remembered(app, db_session):
    _, _, task = await setup(db_session, app, chat_type="group")
    await remember_links(db_session, task.id, f"引用：{DOC}\n正文 {OTHER}")
    await db_session.commit()
    row = await db_session.get(Task, task.id, populate_existing=True)
    assert row.payload["doc_links"] == ["DocToken1234", "OtherToken99"]
    assert row.payload["bot_key"]  # 其余字段不动


async def test_unknown_speaker_gets_only_the_app_layer(app, db_session, db_engine):
    bot, _, task = await group(db_session, app)
    intake = await intake_for(db_session, bot, task, "看下", SUPPORTED)
    from coreman.core.prompting import Speaker

    intake = replace(intake, speaker=Speaker("human", None, None, None))
    ctx = build_ctx(db_engine, task)
    ctx.turn_notes = []
    _, env = await configure(db_session, ctx, intake, uuid.uuid4(), "", {})
    assert policy.read_capability(ctx.cipher, env[policy.PREFIX + "TOKEN"]).chat_id == "oc_group"

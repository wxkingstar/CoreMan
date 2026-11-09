"""群里读飞书文档：先用应用身份读本群看得到的，读不到再以提问人本人身份读他发过的链接。"""

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
from coreman.core.db.models import (
    FeishuAuthWait,
    FeishuDocConsent,
    InboundEvent,
    OutboxItem,
    Task,
)
from coreman.core.feishu_docs import consent, links, policy
from coreman.core.feishu_docs.links import LinkError
from coreman.core.feishu_personal import service
from coreman.runtime.worker.chat.feishu_docs import configure, remember_links
from tests.api.test_feishu_inflow import device
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
    client, app, db_session, entity, external, readable, monkeypatch
):
    _, _, task = await group(db_session, app)
    monkeypatch.setattr(service.permissions, "app_user_scopes", app_scopes)
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
        device(mock)
        out = value(
            await client.post(
                URL, headers=await headers(app, task), json=rpc("feishu_doc_read", {"url": DOC})
            )
        )
    assert ("content" in out) is readable
    assert route.called is readable
    if not readable:
        # 提问人还没连接飞书：私信发授权卡，记下这一轮，授权完成后自动续接。
        assert out["status"] == "authorization_sent"
        row = (await db_session.scalars(select(FeishuAuthWait))).one()
        assert (row.status, row.chat_id, row.origin_task_id) == (
            "waiting",
            "oc_group",
            task.id,
        )
        items = list(await db_session.scalars(select(OutboxItem)))
        assert [item.target for item in items] == [{"user_id": "human"}]
        assert "accounts.feishu.cn" in str(items[0].payload["card"])


async def app_scopes(*args, **kwargs):
    return ["docx:document:readonly", "docx:document", "offline_access"]


def user_fetch(mock, status=200):
    body = {"code": 0, "data": {"document": {"content": "# 方案"}}}
    if status != 200:
        body = {"code": 1063002, "msg": "permission denied"}
    return mock.post(f"{OPEN}/docs_ai/v1/documents/DocToken1234/fetch").mock(
        return_value=httpx.Response(status, json=body)
    )


async def connected(db_session, app, bot, user, **kw):
    await grant(
        db_session,
        app,
        bot,
        user,
        authorization_level="all",
        scopes=READ_SCOPES,
        requested_scopes=READ_SCOPES,
        **kw,
    )


async def test_a_connected_asker_reads_directly_without_a_card(client, app, db_session):
    bot, user, task = await group(db_session, app)
    await connected(db_session, app, bot, user)
    with respx.mock as mock:
        tenant(mock)
        permissions(mock)
        fetched = user_fetch(mock)
        for path in ("members", "public"):
            mock.get(f"{OPEN}/drive/v1/permissions/OtherToken99/{path}").mock(
                return_value=httpx.Response(403, json={"code": 1063002, "msg": "denied"})
            )
        out = value(
            await client.post(
                URL, headers=await headers(app, task), json=rpc("feishu_doc_read", {"url": DOC})
            )
        )
        # 不是本群有人发过的链接，不能以提问人身份读。
        other = value(
            await client.post(
                URL, headers=await headers(app, task), json=rpc("feishu_doc_read", {"url": OTHER})
            )
        )
    assert out["content"] == "# 方案" and out["read_as"] == "requester"
    assert out["can_offer_share"] is True
    assert fetched.calls[0].request.headers["authorization"] == "Bearer user-secret"
    assert "user-secret" not in json.dumps(out)
    assert other["reason"] == "link_not_from_chat"
    assert (await db_session.scalars(select(FeishuDocConsent))).all() == []


async def test_links_posted_earlier_in_this_group_can_be_read(client, app, db_session):
    bot, user, earlier = await group(db_session, app)
    await connected(db_session, app, bot, user)
    await tasks.finish(db_session, earlier.id, status="succeeded")
    await db_session.commit()
    task = await chat_task(
        db_session, bot, "按文档改", sender="human", chat_type="group", chat_id="oc_group"
    )
    event = await db_session.get(InboundEvent, task.inbound_event_id)
    origin = await db_session.get(InboundEvent, earlier.inbound_event_id)
    event.payload, event.sender_open_id = origin.payload, origin.sender_open_id
    await db_session.commit()
    with respx.mock as mock:
        tenant(mock)
        permissions(mock)
        user_fetch(mock)
        out = value(
            await client.post(
                URL, headers=await headers(app, task), json=rpc("feishu_doc_read", {"url": DOC})
            )
        )
    assert out["read_as"] == "requester"


async def test_reasons_say_what_is_missing(client, app, db_session, monkeypatch):
    bot, user, task = await group(db_session, app)
    await connected(db_session, app, bot, user)
    with respx.mock as mock:
        tenant(mock)
        permissions(mock)
        user_fetch(mock, status=403)
        cannot = value(
            await client.post(
                URL, headers=await headers(app, task), json=rpc("feishu_doc_read", {"url": DOC})
            )
        )
    assert cannot["reason"] == "asker_cannot_open" and "申请权限" in cannot["hint"]
    await connected_scopes(db_session, ["search:message"])
    monkeypatch.setattr(service.permissions, "app_user_scopes", app_scopes)
    with respx.mock as mock:
        tenant(mock)
        permissions(mock)
        lacking = value(
            await client.post(
                URL, headers=await headers(app, task), json=rpc("feishu_doc_read", {"url": DOC})
            )
        )
    # 授权不含文档接口：只给提问人私信一张升级卡，群里不放任何链接。
    assert lacking["status"] == "upgrade_offered"
    wait = (await db_session.scalars(select(FeishuAuthWait))).one()
    assert (wait.status, wait.chat_id) == ("offered", "oc_group")
    items = list(await db_session.scalars(select(OutboxItem)))
    assert [item.target for item in items] == [{"user_id": "human"}]
    # 应用本身没开这项权限：让提问人联系机器人管理员。
    monkeypatch.setattr(service.permissions, "app_user_scopes", no_scopes)
    with respx.mock as mock:
        tenant(mock)
        permissions(mock)
        missing = value(
            await client.post(
                URL, headers=await headers(app, task), json=rpc("feishu_doc_read", {"url": DOC})
            )
        )
    assert missing["status"] == "app_permission_missing"
    assert len(list(await db_session.scalars(select(OutboxItem)))) == 1


async def no_scopes(*args, **kwargs):
    return []


async def connected_scopes(db_session, scopes):
    from coreman.core.db.models import FeishuPersonalGrant

    row = (await db_session.scalars(select(FeishuPersonalGrant))).one()
    row.scopes = row.requested_scopes = scopes
    await db_session.commit()


async def test_offer_share_posts_a_share_only_card(client, app, db_session):
    bot, user, task = await group(db_session, app)
    await connected(db_session, app, bot, user)
    with respx.mock as mock:
        tenant(mock)
        outside = value(
            await client.post(
                URL,
                headers=await headers(app, task),
                json=rpc("feishu_doc_offer_share", {"urls": [OTHER]}),
            )
        )
        asked = value(
            await client.post(
                URL,
                headers=await headers(app, task),
                json=rpc("feishu_doc_offer_share", {"urls": [DOC]}),
            )
        )
    assert outside["error"] == "link_not_from_chat"
    assert asked["status"] == "access_requested"
    row = (await db_session.scalars(select(FeishuDocConsent))).one()
    assert (row.status, row.tokens, row.share_offered) == ("pending", ["DocToken1234"], True)
    item = await db_session.get(OutboxItem, row.card_outbox_id)
    card = json.dumps(item.payload["card"], ensure_ascii=False)
    assert "<at id=ou_human></at>" in card and DOC in card
    assert consent.SHARE in card and consent.ONCE not in card


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


async def test_an_old_cards_click_is_still_recorded(app, db_session, db_engine):
    bot, user, task = await group(db_session, app)
    row = await pending_card(db_session, app, bot, user, task)
    turn = await click(db_session, bot, task, consent.ONCE)
    intake = await intake_for(db_session, bot, turn, consent.ONCE, SUPPORTED)
    ctx = build_ctx(db_engine, turn)
    ctx.turn_notes = []
    prompt, env = await configure(db_session, ctx, intake, uuid.uuid4(), "你是助理", {})
    await db_session.commit()
    assert "## 群里的飞书文档" in prompt and env[policy.PREFIX + "URL"].endswith(URL)
    assert "不要用浏览器" in prompt and "不用另外申请" in prompt
    assert consent.ONCE in ctx.turn_notes[0]
    await db_session.refresh(row)
    assert row.status == "approved" and row.decided_at is not None


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
    assert out["error"] == "no_access" and out["reason"] == "unknown_speaker"


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
    assert row.status == "declined" and "不把" in ctx.turn_notes[0]


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


async def test_quoted_and_forwarded_links_are_remembered(app, db_session):
    _, _, task = await setup(db_session, app, chat_type="group")
    parts = [
        {"type": "text", "text": "按文档改"},
        {"type": "quote", "kind": "text", "text": f"结构：{DOC}业务：{OTHER}", "refs": []},
    ]
    await remember_links(db_session, task.id, "按文档改", parts)
    await db_session.commit()
    row = await db_session.get(Task, task.id, populate_existing=True)
    assert row.payload["doc_links"] == ["DocToken1234", "OtherToken99"]


async def test_authorization_finished_later_resumes_the_turn(app, db_session, db_engine):
    from coreman.core.db.models import FeishuPersonalGrant
    from coreman.core.feishu_docs import resume
    from coreman.core.feishu_personal import service

    bot, user, task = await group(db_session, app)
    await connected(db_session, app, bot, user)
    row = (await db_session.scalars(select(FeishuPersonalGrant))).one()
    row.status, row.token_enc = "pending", None
    row.pending_enc = app.state.cipher.encrypt(
        json.dumps({"device_code": "device", "url": "https://accounts.feishu.cn/verify"}),
        service._aad(row, "pending_enc"),
    )
    row.pending_expires_at = consent.utcnow() + consent.PENDING_TTL
    row.next_poll_at = None
    await db_session.commit()
    with respx.mock as mock:
        tenant(mock)
        permissions(mock)
        mock.post(f"{OPEN}/authen/v2/oauth/token").mock(
            return_value=httpx.Response(200, json={"error": "authorization_pending"})
        )
        async with app.state.session_factory() as session:
            scope = await policy.scope(
                session, policy.Capability(task.id, "oc_group", uuid.uuid4())
            )
            state = await access_state(session, app, scope)
            await session.commit()
    assert state == "authorization_pending"
    waiting = FeishuAuthWait(
        bot_id=bot.id,
        user_id=user.id,
        chat_id="oc_group",
        origin_task_id=task.id,
        need="general",
        status="waiting",
        expires_at=consent.utcnow() + resume.AWAIT_TTL,
    )
    db_session.add(waiting)
    row = await db_session.get(FeishuPersonalGrant, (bot.id, user.id), populate_existing=True)
    row.next_poll_at = None  # 到了下一次该查的时候
    await db_session.commit()
    await tasks.finish(db_session, task.id, status="succeeded")
    await db_session.commit()
    with respx.mock as mock:
        mock.post(f"{OPEN}/authen/v2/oauth/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "user-secret",
                    "refresh_token": "refresh",
                    "expires_in": 7200,
                    "scope": " ".join(READ_SCOPES),
                },
            )
        )
        mock.get(f"{OPEN}/authen/v1/user_info").mock(
            return_value=httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "user_id": "human",
                        "open_id": "ou_human",
                        "tenant_key": "tenant-test",
                    },
                },
            )
        )
        async with app.state.session_factory() as session:
            assert await resume.tick(session, consent.utcnow(), app.state.cipher) == 1
            await session.commit()
    await db_session.refresh(waiting)
    assert waiting.status == "resumed"
    again = (
        await db_session.scalars(
            select(Task).where(Task.payload[resume.PAYLOAD_KEY].astext == str(waiting.id))
        )
    ).one()
    assert again.inbound_event_id == task.inbound_event_id and again.kind == "chat"
    assert again.payload["serialize_session"] is True
    assert again.payload["doc_links"] == task.payload["doc_links"]
    again.status = "running"  # 被 worker 认领后开轮
    await db_session.commit()
    intake = await intake_for(db_session, bot, again, "看下", SUPPORTED)
    ctx = build_ctx(db_engine, again)
    ctx.turn_notes = []
    await configure(db_session, ctx, intake, uuid.uuid4(), "", {})
    assert "自动续接" in ctx.turn_notes[-1]


async def connected_level(db_session, level):
    from coreman.core.db.models import FeishuPersonalGrant

    row = (await db_session.scalars(select(FeishuPersonalGrant))).one()
    row.authorization_level = level
    await db_session.commit()


async def waiting_row(db_session, app, bot, user, task, *, status, need, level):
    from coreman.core.feishu_docs import resume

    await connected(db_session, app, bot, user)
    await connected_level(db_session, level)
    row = FeishuAuthWait(
        bot_id=bot.id,
        user_id=user.id,
        chat_id="oc_group",
        origin_task_id=task.id,
        need=need,
        status=status,
        expires_at=consent.utcnow() + resume.AWAIT_TTL,
    )
    db_session.add(row)
    await tasks.finish(db_session, task.id, status="succeeded")
    await db_session.commit()
    return row


async def test_an_offered_wait_is_not_resumed_and_asks_nothing(app, db_session):
    from coreman.core.feishu_docs import resume

    bot, user, task = await group(db_session, app)
    row = await waiting_row(
        db_session, app, bot, user, task, status="offered", need="general", level="all"
    )
    with respx.mock:  # 没登记任何路由：要是去查飞书就会报错
        async with app.state.session_factory() as session:
            assert await resume.tick(session, consent.utcnow(), app.state.cipher) == 0
            await session.commit()
    await db_session.refresh(row)
    assert row.status == "offered"


async def test_a_wait_resumes_only_when_the_tier_is_enough(app, db_session):
    from coreman.core.feishu_docs import resume

    bot, user, task = await group(db_session, app)
    row = await waiting_row(
        db_session,
        app,
        bot,
        user,
        task,
        status="waiting",
        need="send_messages",
        level="all_except_send",
    )
    async with app.state.session_factory() as session:
        assert await resume.tick(session, consent.utcnow(), app.state.cipher) == 0
        await session.commit()
    await db_session.refresh(row)
    assert row.status == "waiting"


async def access_state(session, app, scope):
    from coreman.core.feishu_docs import access

    return (await access.asker_state(session, app.state.cipher, scope)).status

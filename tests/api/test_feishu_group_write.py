"""群聊以提问人身份写入。"""

import httpx
import respx
from sqlalchemy import select

from coreman.core.db.models import FeishuGroupAction, OutboxItem
from coreman.core.feishu_docs import policy, tools, write_policy, write_tools
from tests.api.test_feishu_group_docs import DOC, OPEN, group, headers
from tests.api.test_feishu_personal import grant, rpc, value

URL = "/api/runtime/feishu-docs/mcp"
WRITE = ["docx:document", "drive:drive", "docx:document:create", "wiki:node:read", "wiki:wiki"]
WIKI = "https://example.feishu.cn/wiki/WikiNode1234"
BLOCKS = {"document_id": "DocToken1234", "blocks": [{"type": "text", "text": "hello"}]}


async def connected(db_session, app, bot, user):
    row = await grant(db_session, app, bot, user)
    row.authorization_level, row.requested_scopes, row.scopes = "all_except_send", WRITE, WRITE
    await db_session.commit()


def append(mock, status=200, code=0):
    return mock.post(f"{OPEN}/docx/v1/documents/DocToken1234/blocks/DocToken1234/children").mock(
        return_value=httpx.Response(status, json={"code": code, "data": {"children": []}})
    )


def wiki_node(mock, obj_token):
    return mock.get(f"{OPEN}/wiki/v2/spaces/get_node").mock(
        return_value=httpx.Response(
            200, json={"code": 0, "data": {"node": {"obj_type": "docx", "obj_token": obj_token}}}
        )
    )


async def call(client, app, task, name, arguments):
    return value(
        await client.post(URL, headers=await headers(app, task), json=rpc(name, arguments))
    )


async def test_append_to_the_askers_link_runs_as_the_asker(client, app, db_session):
    bot, user, task = await group(db_session, app, f"写到 {DOC}")
    await connected(db_session, app, bot, user)
    with respx.mock as mock:
        route = append(mock)
        out = await call(client, app, task, "feishu_append_document", BLOCKS)
    assert "error" not in out
    assert route.calls[0].request.headers["authorization"] == "Bearer user-secret"
    row = (await db_session.scalars(select(FeishuGroupAction))).one()
    assert (row.user_id, row.status, row.target) == (user.id, "succeeded", "DocToken1234")


async def test_same_call_twice_runs_once(client, app, db_session):
    bot, user, task = await group(db_session, app, f"写到 {DOC}")
    await connected(db_session, app, bot, user)
    with respx.mock as mock:
        route = append(mock)
        for _ in range(2):
            out = await call(client, app, task, "feishu_append_document", BLOCKS)
    assert route.call_count == 1 and out["status"] == "already_done"


async def test_overwrite_is_refused(client, app, db_session):
    bot, user, task = await group(db_session, app, f"改 {DOC}")
    await connected(db_session, app, bot, user)
    out = await call(
        client,
        app,
        task,
        "feishu_edit_document",
        {"document_id": "DocToken1234", "command": "overwrite", "content": "x"},
    )
    assert out["error"] == "overwrite_not_allowed_in_group"


async def test_tools_outside_the_whitelist_are_unavailable(client, app, db_session):
    bot, user, task = await group(db_session, app)
    await connected(db_session, app, bot, user)
    out = await call(client, app, task, "feishu_mail_search", {})
    assert out["error"] == "invalid_tool_or_arguments"


async def test_without_authorization_a_private_card_goes_out(client, app, db_session, monkeypatch):
    from coreman.core.feishu_personal import service

    bot, user, task = await group(db_session, app, f"写到 {DOC}")

    async def scopes(*a, **k):
        return WRITE + ["offline_access"]

    monkeypatch.setattr(service.permissions, "app_user_scopes", scopes)
    with respx.mock as mock:
        mock.post("https://accounts.feishu.cn/oauth/v1/device_authorization").mock(
            return_value=httpx.Response(
                200,
                json={
                    "device_code": "d",
                    "verification_uri_complete": "https://accounts.feishu.cn/v",
                    "expires_in": 600,
                    "interval": 5,
                },
            )
        )
        out = await call(client, app, task, "feishu_append_document", BLOCKS)
    assert out["status"] == "authorization_sent"
    item = (await db_session.scalars(select(OutboxItem))).one()
    assert item.target == {"user_id": "human"}


async def test_a_target_not_from_the_askers_message_needs_confirmation(client, app, db_session):
    bot, user, task = await group(db_session, app, "写到那篇文档")
    await connected(db_session, app, bot, user)
    with respx.mock as mock:
        route = append(mock)
        out = await call(client, app, task, "feishu_append_document", BLOCKS)
    assert out["status"] == "pending_confirmation" and route.call_count == 0


async def test_wiki_link_trusts_only_the_nodes_actual_document(client, app, db_session):
    bot, user, task = await group(db_session, app, f"写到 {WIKI}")
    await connected(db_session, app, bot, user)
    with respx.mock as mock:
        wiki_node(mock, "DocToken1234")
        route = append(mock)
        out = await call(client, app, task, "feishu_append_document", BLOCKS)
    assert "error" not in out and route.call_count == 1


async def test_wiki_link_does_not_vouch_for_another_document(client, app, db_session):
    bot, user, task = await group(db_session, app, f"写到 {WIKI}")
    await connected(db_session, app, bot, user)
    with respx.mock as mock:
        wiki_node(mock, "SomeOtherDoc99")
        route = append(mock)
        out = await call(client, app, task, "feishu_append_document", BLOCKS)
    assert out["status"] == "pending_confirmation" and route.call_count == 0


async def test_a_document_created_this_turn_can_be_written(client, app, db_session):
    bot, user, task = await group(db_session, app, "新建一篇")
    await connected(db_session, app, bot, user)
    with respx.mock as mock:
        mock.post(f"{OPEN}/docx/v1/documents").mock(
            return_value=httpx.Response(
                200, json={"code": 0, "data": {"document": {"document_id": "DocToken1234"}}}
            )
        )
        route = append(mock)
        await call(client, app, task, "feishu_create_document", {"title": "周报"})
        out = await call(client, app, task, "feishu_append_document", BLOCKS)
    assert "error" not in out and route.call_count == 1
    rows = list(await db_session.scalars(select(FeishuGroupAction)))
    assert {(r.category, r.target) for r in rows} == {
        ("create", "DocToken1234"),
        ("target", "DocToken1234"),
    }


async def test_a_failed_write_is_audited_and_can_be_retried(client, app, db_session):
    bot, user, task = await group(db_session, app, f"写到 {DOC}")
    await connected(db_session, app, bot, user)
    with respx.mock as mock:
        append(mock, code=1770002)
        out = await call(client, app, task, "feishu_append_document", BLOCKS)
    assert "error" in out
    row = (await db_session.scalars(select(FeishuGroupAction))).one()
    assert row.status == "failed"
    with respx.mock as mock:
        route = append(mock)
        out = await call(client, app, task, "feishu_append_document", BLOCKS)
    assert "error" not in out and route.call_count == 1


async def test_write_cap_per_turn(client, app, db_session):
    bot, user, task = await group(db_session, app, f"写到 {DOC}")
    await connected(db_session, app, bot, user)
    for index in range(write_policy.MAX_WRITES_PER_TURN):
        db_session.add(
            FeishuGroupAction(
                bot_id=bot.id,
                user_id=user.id,
                chat_id="oc_group",
                task_id=task.id,
                tool="feishu_create_document",
                category="create",
                status="succeeded",
                args_hash=str(index),
            )
        )
    await db_session.commit()
    with respx.mock as mock:
        route = append(mock)
        out = await call(client, app, task, "feishu_append_document", BLOCKS)
    assert out["error"] == "write_limit_reached" and route.call_count == 0


async def test_unknown_speaker_is_refused_first(app, db_session):
    bot, _, task = await group(db_session, app, f"写到 {DOC}")
    scope = policy.DocsScope(task, bot, None, "oc_group", None)
    out = await write_tools.dispatch(
        db_session, app.state.cipher, scope, "feishu_append_document", BLOCKS, None
    )
    assert out == {"error": "no_access", "reason": "unknown_speaker"}


async def test_send_kind_tool_asks_for_send_authorization(client, app, db_session, monkeypatch):
    bot, user, task = await group(db_session, app, f"共享 {DOC}")
    await connected(db_session, app, bot, user)
    seen = {}

    async def request(session, cipher, scope, **kw):
        seen.update(kw)
        return {"status": "upgrade_offered"}

    monkeypatch.setattr(write_tools.inflow, "request", request)
    out = await call(
        client,
        app,
        task,
        "feishu_share_document",
        {
            "token": "DocToken1234",
            "doc_type": "docx",
            "member_open_ids": ["ou_other"],
            "permission": "view",
        },
    )
    assert out["status"] == "upgrade_offered"
    assert seen["need"] == "send_messages" and seen["endpoints_needed"] == ("drive.share",)


async def test_general_tool_asks_for_general_authorization(client, app, db_session, monkeypatch):
    bot, user, task = await group(db_session, app, f"写到 {DOC}")
    seen = {}

    async def request(session, cipher, scope, **kw):
        seen.update(kw)
        return {"status": "authorization_sent"}

    monkeypatch.setattr(write_tools.inflow, "request", request)
    await call(client, app, task, "feishu_append_document", BLOCKS)
    assert seen["need"] == "general"


async def test_already_connected_is_not_reported_as_success(client, app, db_session, monkeypatch):
    bot, user, task = await group(db_session, app, f"写到 {DOC}")

    async def request(session, cipher, scope, **kw):
        return {"status": "already_connected"}

    monkeypatch.setattr(write_tools.inflow, "request", request)
    out = await call(client, app, task, "feishu_append_document", BLOCKS)
    assert out["error"] == "no_access"


def test_tool_listing_is_the_whitelist_for_everyone():
    names = {item["name"] for item in tools.definitions()}
    assert set(write_policy.GROUP_TOOLS) <= names
    assert "feishu_mail_search" not in names

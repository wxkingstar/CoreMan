"""群聊以提问人身份写入。"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import respx
from sqlalchemy import select

from coreman.core.bus import tasks
from coreman.core.db.models import (
    FeishuAuthWait,
    FeishuGroupAction,
    FeishuPersonalGrant,
    InboundEvent,
    InteractionState,
    OutboxItem,
    Task,
    User,
    UserIdentity,
)
from coreman.core.feishu_docs import confirm, policy, resume, tools, write_policy, write_tools
from coreman.core.timeutils import utcnow
from coreman.runtime.worker.card_actions import CardActionHandler
from coreman.runtime.worker.chat.feishu_docs import RULES, configure
from coreman.runtime.worker.chat.personal import configure as personal_configure
from coreman.runtime.worker.chat.personal import feishu_guidance
from tests.api.test_feishu_group_docs import DOC, OPEN, SUPPORTED, group, headers
from tests.api.test_feishu_personal import grant, rpc, value
from tests.api.test_feishu_personal_worker import intake_for
from tests.integration.worker_helpers import build_ctx

URL = "/api/runtime/feishu-docs/mcp"
WRITE = [
    "docx:document",
    "drive:drive",
    "docx:document:create",
    "wiki:node:read",
    "wiki:wiki",
    "task:task:read",
    "calendar:calendar",
]
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


async def test_prepare_upload_is_not_a_write(client, app, db_session):
    bot, user, task = await group(db_session, app, "传个文件")
    await connected(db_session, app, bot, user)
    args = {"filename": "a.pdf"}
    first = await call(client, app, task, "feishu_prepare_upload", args)
    second = await call(client, app, task, "feishu_prepare_upload", args)
    assert first["upload_url"] and second["upload_url"]
    assert first["content_trust"] == "external_untrusted_data"
    assert list(await db_session.scalars(select(FeishuGroupAction))) == []


async def test_locate_output_is_trimmed_and_marked(client, app, db_session):
    bot, user, task = await group(db_session, app, "看看任务")
    await connected(db_session, app, bot, user)
    with respx.mock as mock:
        mock.get(f"{OPEN}/task/v2/tasks").mock(
            return_value=httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"items": [{"guid": "g1", "summary": "周报", "description": "正文"}]},
                },
            )
        )
        out = await call(client, app, task, "feishu_task_list", {})
    assert out["items"] == [{"guid": "g1", "summary": "周报"}]
    assert out["content_trust"] == "external_untrusted_data"


async def test_create_with_attendees_needs_confirmation(client, app, db_session):
    bot, user, task = await group(db_session, app, "约个会")
    await connected(db_session, app, bot, user)
    with respx.mock as mock:
        route = mock.post(url__regex=r".*/calendar/v4/.*").mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {}})
        )
        out = await call(
            client,
            app,
            task,
            "feishu_calendar_create_event",
            {
                "calendar_id": "cal_1",
                "summary": "周会",
                "start_time": "2026-11-10T09:00:00+08:00",
                "end_time": "2026-11-10T10:00:00+08:00",
                "attendee_open_ids": ["ou_other"],
                "idempotency_key": "k1",
            },
        )
    assert out["status"] == "pending_confirmation" and route.call_count == 0


def test_tool_listing_is_the_whitelist_for_everyone():
    names = {item["name"] for item in tools.definitions()}
    assert set(write_policy.GROUP_TOOLS) <= names
    assert "feishu_mail_search" not in names


DELETE = {"calendar_id": "cal", "event_id": "evt"}
DELETE_URL = r".*/calendars/cal/events/evt.*"


def delete_route(mock, status=200, code=0):
    return mock.delete(url__regex=DELETE_URL).mock(
        return_value=httpx.Response(status, json={"code": code, "data": {}})
    )


async def pending_delete(client, app, db_session, text=f"帮我总结 {DOC}", args=None):
    bot, user, task = await group(db_session, app, text)
    await connected(db_session, app, bot, user)
    with respx.mock as mock:
        route = delete_route(mock)
        out = await call(client, app, task, "feishu_calendar_delete_event", args or DELETE)
    assert out["status"] == "pending_confirmation" and route.call_count == 0
    state = await db_session.scalar(
        select(InteractionState).where(InteractionState.kind == confirm.KIND)
    )
    return bot, user, task, state


async def press(
    session,
    bot,
    original,
    state_id,
    verb="confirm",
    *,
    user_id="human",
    open_id="ou_human",
    chat_id="oc_group",
):
    card = await session.scalar(select(OutboxItem).where(OutboxItem.kind == "send"))
    card.status = "sent"
    card.payload = {**card.payload, "_feishu_message_id": "om_card"}
    action = {
        "task_id": f"personal:{original.id}",
        "event_key": confirm.CARD_EVENT,
        "level": f"{verb}:{state_id}",
    }
    event = InboundEvent(
        bot_id=bot.id,
        platform="feishu",
        platform_msg_id=str(uuid.uuid4()),
        kind="card_action",
        chat_type="group",
        chat_id=chat_id,
        sender_platform_user_id=user_id,
        sender_open_id=open_id,
        reply_context={"chat_id": chat_id, "message_id": "om_card"},
        payload={
            "raw": {
                "header": {
                    "event_type": "card.action.trigger",
                    "app_id": "cli_test",
                    "tenant_key": "tenant-test",
                },
                "event": {
                    "operator": {"user_id": user_id, "open_id": open_id},
                    "context": {"open_chat_id": chat_id, "open_message_id": "om_card"},
                    "action": {"value": action},
                },
            }
        },
    )
    session.add(event)
    await session.flush()
    task = await tasks.enqueue(
        session,
        tasks.NewTask(
            bot_id=bot.id,
            kind="card_action",
            lane="fast",
            payload={"card_action": action, "platform_user_id": user_id},
            session_key=chat_id,
            inbound_event_id=event.id,
            dedupe_key=f"click:{event.id}",
        ),
    )
    task.status = "running"
    await session.commit()
    return task


async def run_click(db_session, db_engine, pressed):
    await CardActionHandler().run(build_ctx(db_engine, pressed))
    await db_session.execute(select(InteractionState).execution_options(populate_existing=True))
    return (await db_session.get(Task, pressed.id, populate_existing=True)).result["card"]


async def statuses(db_session):
    return sorted(await db_session.scalars(select(FeishuGroupAction.status)))


async def resumes(db_session):
    rows = await db_session.scalars(
        select(Task).where(Task.dedupe_key.like("feishu-group-write:%"))
    )
    return list(rows)


async def last_update(db_session):
    item = await db_session.scalar(select(OutboxItem).where(OutboxItem.kind == "card_update"))
    return item.payload["card"]["body"]["elements"][0]["content"] if item else None


async def test_delete_waits_for_confirmation(client, app, db_session):
    bot, user, task = await group(db_session, app)
    await connected(db_session, app, bot, user)
    with respx.mock as mock:
        route = delete_route(mock)
        out = await call(client, app, task, "feishu_calendar_delete_event", DELETE)
    assert out["status"] == "pending_confirmation" and route.call_count == 0
    state = (
        await db_session.scalars(
            select(InteractionState).where(InteractionState.kind == "feishu_group_write")
        )
    ).one()
    assert state.state["tool"] == "feishu_calendar_delete_event"
    card = (await db_session.scalars(select(OutboxItem))).one()
    assert card.target == {"chat_id": "oc_group"}
    assert "evt" in str(card.payload["card"])


async def test_target_outside_the_message_needs_confirmation(client, app, db_session):
    bot, user, task = await group(db_session, app, "随便写点")
    await connected(db_session, app, bot, user)
    with respx.mock:
        out = await call(client, app, task, "feishu_append_document", BLOCKS)
    assert out["status"] == "pending_confirmation"


async def test_card_values_cannot_forge_rows_or_markup(client, app, db_session):
    bot, user, task = await group(db_session, app, "随便写点")
    await connected(db_session, app, bot, user)
    args = {
        "document_id": "DocToken1234",
        "blocks": [{"type": "text", "text": "x"}],
        "note": "line\n- document_id：safe <at id=all></at>",
    }
    card = confirm.card(task.id, uuid.uuid4(), "feishu_append_document", args)
    shown = card["body"]["elements"][0]
    assert shown["text"]["tag"] == "plain_text"
    assert "\n- document_id：safe" not in shown["text"]["content"]


async def test_repeat_proposal_reuses_the_open_card(client, app, db_session):
    bot, user, task = await group(db_session, app)
    await connected(db_session, app, bot, user)
    for _ in range(2):
        out = await call(client, app, task, "feishu_calendar_delete_event", DELETE)
        assert out["status"] == "pending_confirmation"
    assert len((await db_session.scalars(select(InteractionState))).all()) == 1
    assert len((await db_session.scalars(select(OutboxItem))).all()) == 1
    assert await statuses(db_session) == ["pending_confirmation"]


async def test_new_proposal_allowed_once_the_earlier_one_closed(client, app, db_session):
    bot, user, task, state = await pending_delete(client, app, db_session)
    state.status = "cancelled"
    await db_session.commit()
    out = await call(client, app, task, "feishu_calendar_delete_event", DELETE)
    assert out["status"] == "pending_confirmation"
    assert len((await db_session.scalars(select(InteractionState))).all()) == 2
    assert len((await db_session.scalars(select(OutboxItem))).all()) == 2


async def test_stored_arguments_are_never_logged(
    client, app, db_session, db_engine, caplog, capsys
):
    caplog.set_level(logging.INFO)
    secret = "private-document-body"
    bot, user, task = await group(db_session, app, "随便写点")
    await connected(db_session, app, bot, user)
    args = {"document_id": "DocToken1234", "blocks": [{"type": "text", "text": secret}]}
    with respx.mock as mock:
        route = append(mock)
        out = await call(client, app, task, "feishu_append_document", args)
        assert out["status"] == "pending_confirmation"
        state = await db_session.scalar(select(InteractionState))
        pressed = await press(db_session, bot, task, state.id)
        assert await run_click(db_session, db_engine, pressed) == "feishu_group_write_done"
    assert route.call_count == 1
    assert secret not in caplog.text and secret not in capsys.readouterr().out


def test_long_values_are_truncated_on_the_card():
    text = confirm._lines({"content": "x" * 600, "short": "y"})
    long_row, short_row = text.split("\n")
    assert long_row == "- content：" + '"' + "x" * (confirm.MAX_VALUE - 1) + "…"
    assert short_row == '- short："y"'


async def test_asker_confirms_and_it_runs_once_as_the_asker(client, app, db_session, db_engine):
    bot, user, task, state = await pending_delete(client, app, db_session)
    pressed = await press(db_session, bot, task, state.id)
    with respx.mock as mock:
        route = delete_route(mock)
        result = await run_click(db_session, db_engine, pressed)
    assert result == "feishu_group_write_done" and route.call_count == 1
    assert route.calls[0].request.headers["authorization"] == "Bearer user-secret"
    assert state.status == "submitted"
    assert await statuses(db_session) == ["pending_confirmation", "succeeded"]
    [resume] = await resumes(db_session)
    assert confirm.RESULT_KEY in resume.payload and resume.inbound_event_id == task.inbound_event_id
    assert resume.session_key == task.session_key and resume.payload["serialize_session"]
    assert "已确认" in await last_update(db_session)


async def test_someone_else_cannot_confirm(client, app, db_session, db_engine):
    bot, user, task, state = await pending_delete(client, app, db_session)
    other = User(login_name="someone-else", display_name="Other", source="sync")
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        UserIdentity(user_id=other.id, platform="feishu", platform_user_id="u2", open_id="ou_2")
    )
    pressed = await press(db_session, bot, task, state.id, user_id="u2", open_id="ou_2")
    with respx.mock as mock:
        route = delete_route(mock)
        result = await run_click(db_session, db_engine, pressed)
    assert result == "ignored" and route.call_count == 0 and state.status == "open"
    assert await last_update(db_session) is None


async def test_forged_operator_is_ignored(client, app, db_session, db_engine):
    bot, user, task, state = await pending_delete(client, app, db_session)
    pressed = await press(db_session, bot, task, state.id, open_id="ou_forged")
    with respx.mock as mock:
        route = delete_route(mock)
        result = await run_click(db_session, db_engine, pressed)
    assert result == "ignored" and route.call_count == 0 and state.status == "open"


async def test_click_in_another_chat_is_ignored(client, app, db_session, db_engine):
    bot, user, task, state = await pending_delete(client, app, db_session)
    card = await db_session.scalar(select(OutboxItem).where(OutboxItem.kind == "send"))
    card.target = {"chat_id": "oc_elsewhere"}
    pressed = await press(db_session, bot, task, state.id, chat_id="oc_elsewhere")
    with respx.mock as mock:
        route = delete_route(mock)
        result = await run_click(db_session, db_engine, pressed)
    assert result == "ignored" and route.call_count == 0 and state.status == "open"


async def test_state_from_another_task_is_ignored(client, app, db_session, db_engine):
    bot, user, task, state = await pending_delete(client, app, db_session)
    state.state = {**state.state, "origin_task_id": task.id + 1000}
    await db_session.commit()
    pressed = await press(db_session, bot, task, state.id)
    with respx.mock as mock:
        route = delete_route(mock)
        result = await run_click(db_session, db_engine, pressed)
    assert result == "ignored" and route.call_count == 0


async def test_card_not_sent_by_us_is_ignored(client, app, db_session, db_engine):
    bot, user, task, state = await pending_delete(client, app, db_session)
    pressed = await press(db_session, bot, task, state.id)
    card = await db_session.scalar(select(OutboxItem).where(OutboxItem.kind == "send"))
    card.payload = {**card.payload, "_feishu_message_id": "om_other"}
    await db_session.commit()
    with respx.mock as mock:
        route = delete_route(mock)
        result = await run_click(db_session, db_engine, pressed)
    assert result == "ignored" and route.call_count == 0 and state.status == "open"


async def test_expired_card_does_not_run(client, app, db_session, db_engine):
    bot, user, task, state = await pending_delete(client, app, db_session)
    state.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    pressed = await press(db_session, bot, task, state.id)
    with respx.mock as mock:
        route = delete_route(mock)
        result = await run_click(db_session, db_engine, pressed)
    assert result == "expired" and route.call_count == 0 and state.status == "expired"
    assert "过期" in await last_update(db_session)


async def test_repeat_click_runs_once(client, app, db_session, db_engine):
    bot, user, task, state = await pending_delete(client, app, db_session)
    with respx.mock as mock:
        route = delete_route(mock)
        first = await run_click(db_session, db_engine, await press(db_session, bot, task, state.id))
        second = await run_click(
            db_session, db_engine, await press(db_session, bot, task, state.id)
        )
    assert (first, second) == ("feishu_group_write_done", "expired") and route.call_count == 1
    assert len(await resumes(db_session)) == 1


async def test_already_succeeded_write_is_not_repeated(client, app, db_session, db_engine):
    bot, user, task, state = await pending_delete(client, app, db_session)
    db_session.add(
        FeishuGroupAction(
            bot_id=bot.id,
            user_id=user.id,
            chat_id="oc_group",
            task_id=task.id,
            tool=state.state["tool"],
            category="confirm",
            status="succeeded",
            args_hash=state.state["digest"],
        )
    )
    pressed = await press(db_session, bot, task, state.id)
    with respx.mock as mock:
        route = delete_route(mock)
        result = await run_click(db_session, db_engine, pressed)
    assert result == "feishu_group_write_done" and route.call_count == 0
    assert state.status == "submitted"


async def test_revoked_authorization_does_not_run(client, app, db_session, db_engine):
    bot, user, task, state = await pending_delete(client, app, db_session)
    row = await db_session.get(FeishuPersonalGrant, (bot.id, user.id))
    row.status = "revoked"
    await db_session.commit()
    pressed = await press(db_session, bot, task, state.id)
    with respx.mock as mock:
        route = delete_route(mock)
        result = await run_click(db_session, db_engine, pressed)
    assert result == "feishu_group_write_unauthorized" and route.call_count == 0
    assert state.status == "cancelled" and "重新" in await last_update(db_session)


async def test_cancel_records_and_resumes_without_running(client, app, db_session, db_engine):
    bot, user, task, state = await pending_delete(client, app, db_session)
    pressed = await press(db_session, bot, task, state.id, "cancel")
    with respx.mock as mock:
        route = delete_route(mock)
        result = await run_click(db_session, db_engine, pressed)
    assert result == "feishu_group_write_cancelled" and route.call_count == 0
    assert state.status == "cancelled"
    assert await statuses(db_session) == ["cancelled", "pending_confirmation"]
    [resume] = await resumes(db_session)
    assert "取消" in resume.payload[confirm.RESULT_KEY]


async def test_upstream_failure_is_audited_and_reported(client, app, db_session, db_engine):
    bot, user, task, state = await pending_delete(client, app, db_session)
    pressed = await press(db_session, bot, task, state.id)
    with respx.mock as mock:
        route = delete_route(mock, code=190004)
        result = await run_click(db_session, db_engine, pressed)
    assert result == "feishu_group_write_failed" and route.call_count == 1
    assert await statuses(db_session) == ["failed", "pending_confirmation"]
    assert state.status == "submitted"
    [resume] = await resumes(db_session)
    assert "失败" in resume.payload[confirm.RESULT_KEY]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("feishu_mail_search", {}),
        ("not_a_tool", {}),
        (
            "feishu_edit_document",
            {"document_id": "DocToken1234", "command": "overwrite", "content": "x"},
        ),
    ],
)
async def test_stored_state_cannot_run_a_refused_tool(
    client, app, db_session, db_engine, tool, arguments
):
    bot, user, task, state = await pending_delete(client, app, db_session)
    state.state = {**state.state, "tool": tool, "arguments": arguments}
    await db_session.commit()
    pressed = await press(db_session, bot, task, state.id)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route(host="open.feishu.cn").mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {}})
        )
        result = await run_click(db_session, db_engine, pressed)
    assert result == "feishu_group_write_refused" and route.call_count == 0
    assert state.status == "cancelled"
    assert await statuses(db_session) == ["failed", "pending_confirmation"]
    assert "不允许" in await last_update(db_session)


async def test_origin_that_is_not_a_typed_message_cannot_confirm(
    client, app, db_session, db_engine
):
    bot, user, task, state = await pending_delete(client, app, db_session)
    origin = await db_session.get(InboundEvent, task.inbound_event_id)
    origin.kind = "card_action"
    await db_session.commit()
    pressed = await press(db_session, bot, task, state.id)
    with respx.mock as mock:
        route = delete_route(mock)
        result = await run_click(db_session, db_engine, pressed)
    assert result == "ignored" and route.call_count == 0 and state.status == "open"


async def test_resumed_turn_is_told_the_result(client, app, db_session, db_engine):
    bot, user, task, state = await pending_delete(client, app, db_session)
    pressed = await press(db_session, bot, task, state.id, "cancel")
    await run_click(db_session, db_engine, pressed)
    [again] = await resumes(db_session)
    again.status = "running"
    await db_session.commit()
    intake = await intake_for(db_session, bot, again, "看下", SUPPORTED)
    ctx = build_ctx(db_engine, again)
    ctx.turn_notes = []
    await configure(db_session, ctx, intake, uuid.uuid4(), "base", {})
    assert any(note.startswith("- 飞书操作：用户取消") for note in ctx.turn_notes)


async def test_confirm_resume_drops_the_auth_resume_marker(client, app, db_session, db_engine):
    bot, user, task, state = await pending_delete(client, app, db_session)
    task.payload = {**task.payload, resume.PAYLOAD_KEY: "earlier-wait"}
    await db_session.commit()
    pressed = await press(db_session, bot, task, state.id, "cancel")
    await run_click(db_session, db_engine, pressed)
    [again] = await resumes(db_session)
    assert confirm.RESULT_KEY in again.payload and resume.PAYLOAD_KEY not in again.payload


async def test_auth_resume_drops_the_confirm_result(app, db_session):
    bot, user, task = await group(db_session, app)
    await connected(db_session, app, bot, user)
    task.payload = {**task.payload, confirm.RESULT_KEY: "用户取消了这次操作，不要重试。"}
    wait = FeishuAuthWait(
        bot_id=bot.id,
        user_id=user.id,
        chat_id="oc_group",
        origin_task_id=task.id,
        need="general",
        status="waiting",
        expires_at=utcnow() + resume.AWAIT_TTL,
    )
    db_session.add(wait)
    await db_session.commit()
    async with app.state.session_factory() as session:
        assert await resume.tick(session, utcnow(), app.state.cipher) == 1
        await session.commit()
    again = await db_session.scalar(select(Task).where(Task.dedupe_key.like("feishu-auth:%")))
    assert again.payload[resume.PAYLOAD_KEY] == str(wait.id)
    assert confirm.RESULT_KEY not in again.payload


@pytest.mark.parametrize("via", ["kind", "raw"])
async def test_turn_opened_by_a_card_click_refuses_to_propose(app, db_session, via):
    # 经 MCP 入口时 policy.scope 已先挡掉这种回合（403），这里直接测 propose 自身。
    bot, user, task = await group(db_session, app)
    event = await db_session.get(InboundEvent, task.inbound_event_id)
    if via == "kind":
        event.kind = "card_action"
    else:
        raw = event.payload["raw"]
        header = {**raw["header"], "event_type": "card.action.trigger"}
        event.payload = {**event.payload, "raw": {**raw, "header": header}}
    await db_session.commit()
    scope = policy.DocsScope(task, bot, event, "oc_group", user.id)
    tool = "feishu_calendar_delete_event"
    out = await confirm.propose(db_session, scope, tool, DELETE, None, "digest")
    assert out["error"] == "confirmation_unavailable" and out["hint"]
    assert (await db_session.scalars(select(InteractionState))).all() == []
    assert (await db_session.scalars(select(OutboxItem))).all() == []
    assert await statuses(db_session) == []


def test_group_rules_cover_writing():
    for phrase in (
        "以提问人身份",
        "pending_confirmation",
        "authorization_sent",
        "confirmation_unavailable",
        "write_limit_reached",
    ):
        assert phrase in RULES


def test_guidance_never_tells_users_to_send_the_connect_phrase():
    for scheduled in (False, True):
        rules, _ = feishu_guidance(
            SimpleNamespace(status="connected", token_enc=b"x", authorization_level="all"),
            "https://example.com",
            scheduled=scheduled,
        )
        assert "发送“连接飞书”" not in rules
    assert "发送“连接飞书”" not in feishu_guidance(None, "https://example.com", scheduled=False)[0]
    assert "发送“连接飞书”" not in RULES


@pytest.mark.parametrize(
    "excluded", [{"origin": "observe"}, {"origin": "dispatch"}, {"collaboration_phase": "help"}]
)
async def test_write_tools_are_refused_outside_ordinary_group_turns(
    client, app, db_session, excluded
):
    bot, user, task = await group(db_session, app, f"写到 {DOC}")
    await connected(db_session, app, bot, user)
    task.payload = {**task.payload, **excluded}
    await db_session.commit()
    with respx.mock as mock:
        route = append(mock)
        response = await client.post(
            URL,
            headers=await headers(app, task),
            json=rpc("feishu_append_document", BLOCKS),
        )
    assert response.status_code == 403 and route.call_count == 0


async def test_the_resume_note_is_added_for_an_authorized_private_turn(db_session, app, db_engine):
    from tests.api.test_feishu_personal import setup as personal_setup

    bot, user, task = await personal_setup(db_session, app)
    task.payload = {**task.payload, resume.PAYLOAD_KEY: "wait-id"}
    await db_session.commit()
    intake = await intake_for(db_session, bot, task, "继续")
    ctx = build_ctx(db_engine, task)
    ctx.turn_notes = []
    await personal_configure(db_session, ctx, intake, uuid.uuid4(), "p", {})
    assert any("自动续接" in note for note in ctx.turn_notes)

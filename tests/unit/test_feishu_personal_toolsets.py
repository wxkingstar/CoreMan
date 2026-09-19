"""Typed personal tools for every product area: tiers, registered endpoints and request shapes."""

import base64
import email
from email import policy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coreman.core.feishu_apps import manifest
from coreman.core.feishu_personal import endpoints, permissions, sends, service, tools

UUID = "a1b2c3"
# One minimal valid call per tool; a new tool without an entry fails the coverage test.
SAMPLES = {
    "feishu_search_messages": {},
    "feishu_read_messages": {"message_ids": ["om_1"]},
    "feishu_chat_history": {"chat_id": "oc_1"},
    "feishu_send_message": {
        "receive_id": "ou_1",
        "receive_id_type": "open_id",
        "text": "hi",
        "uuid": UUID,
    },
    "feishu_reply_message": {"message_id": "om_1", "text": "hi", "uuid": UUID},
    "feishu_forward_message": {
        "message_id": "om_1",
        "receive_id": "oc_1",
        "receive_id_type": "chat_id",
        "uuid": UUID,
    },
    "feishu_recall_message": {"message_id": "om_1"},
    "feishu_add_reaction": {"message_id": "om_1", "emoji_type": "THUMBSUP"},
    "feishu_pin_message": {"message_id": "om_1"},
    "feishu_list_chats": {"query": "weekly"},
    "feishu_chat_members": {"chat_id": "oc_1"},
    "feishu_create_chat": {"name": "Project", "member_open_ids": ["ou_1"], "uuid": UUID},
    "feishu_add_chat_members": {"chat_id": "oc_1", "member_open_ids": ["ou_1"]},
    "feishu_search_meetings": {},
    "feishu_read_meeting": {"meeting_id": "m1"},
    "feishu_search_minutes": {},
    "feishu_read_minutes": {"minute_token": "t1"},
    "feishu_read_note": {"note_id": "n1"},
    "feishu_calendar_list": {},
    "feishu_calendar_events": {
        "start_time": "2026-09-19T00:00:00+08:00",
        "end_time": "2026-09-20T00:00:00+08:00",
    },
    "feishu_calendar_event": {"event_id": "ev_0"},
    "feishu_calendar_search": {"query": "review"},
    "feishu_calendar_freebusy": {
        "start_time": "2026-09-19T09:00:00+08:00",
        "end_time": "2026-09-19T18:00:00+08:00",
    },
    "feishu_calendar_create_event": {
        "summary": "Sync",
        "start_time": "2026-09-19T10:00:00+08:00",
        "end_time": "2026-09-19T11:00:00+08:00",
        "idempotency_key": UUID,
    },
    "feishu_calendar_update_event": {"event_id": "ev_0", "summary": "Moved"},
    "feishu_calendar_delete_event": {"event_id": "ev_0"},
    "feishu_calendar_add_attendees": {"event_id": "ev_0", "attendee_open_ids": ["ou_1"]},
    "feishu_calendar_reply": {"event_id": "ev_0", "rsvp_status": "accept"},
    "feishu_mail_folders": {},
    "feishu_mail_list": {},
    "feishu_mail_search": {"query": "invoice"},
    "feishu_mail_read": {"message_id": "TWFpbA=="},
    "feishu_mail_create_draft": {"to": ["a@example.com"], "subject": "Hi", "body": "Hello"},
    "feishu_mail_send": {
        "to": ["a@example.com"],
        "subject": "Hi",
        "body": "Hello",
        "uuid": UUID,
    },
    "feishu_mail_modify": {"message_id": "m1", "mark_read": True},
    "feishu_mail_trash": {"message_id": "m1"},
    "feishu_task_list": {},
    "feishu_task_get": {"task_guid": "t-1"},
    "feishu_task_create": {"summary": "Write report", "client_token": UUID},
    "feishu_task_update": {"task_guid": "t-1", "completed": True},
    "feishu_task_delete": {"task_guid": "t-1"},
    "feishu_tasklists": {},
    "feishu_tasklist_tasks": {"tasklist_guid": "tl-1"},
    "feishu_task_comment": {"task_guid": "t-1", "content": "done"},
    "feishu_search_docs": {"query": "plan"},
    "feishu_drive_files": {},
    "feishu_read_document": {"document_id": "doc1"},
    "feishu_create_document": {"title": "Notes"},
    "feishu_append_document": {"document_id": "doc1", "blocks": [{"text": "hello"}]},
    "feishu_document_comments": {"file_token": "doc1", "file_type": "docx"},
    "feishu_add_document_comment": {"file_token": "doc1", "file_type": "docx", "text": "ok"},
    "feishu_sheet_info": {"spreadsheet_token": "sht1"},
    "feishu_sheet_read": {"spreadsheet_token": "sht1", "ranges": ["s1!A1:B2"]},
    "feishu_sheet_write": {"spreadsheet_token": "sht1", "range": "s1!A1:B1", "values": [[1, "x"]]},
    "feishu_sheet_append": {"spreadsheet_token": "sht1", "range": "s1!A1:B1", "values": [[1, 2]]},
    "feishu_create_sheet": {"title": "Budget"},
    "feishu_base_tables": {"app_token": "app1"},
    "feishu_base_fields": {"app_token": "app1", "table_id": "tbl1"},
    "feishu_base_search": {"app_token": "app1", "table_id": "tbl1"},
    "feishu_base_create_record": {"app_token": "app1", "table_id": "tbl1", "fields": {"A": 1}},
    "feishu_base_update_record": {
        "app_token": "app1",
        "table_id": "tbl1",
        "record_id": "rec1",
        "fields": {"A": 2},
    },
    "feishu_base_delete_record": {"app_token": "app1", "table_id": "tbl1", "record_id": "rec1"},
    "feishu_wiki_spaces": {},
    "feishu_wiki_node": {"token": "wik1"},
    "feishu_wiki_nodes": {"space_id": "sp1"},
    "feishu_wiki_create_node": {"space_id": "sp1", "title": "Page"},
    "feishu_search_users": {"query": "Alex"},
    "feishu_get_user": {"open_id": "ou_1"},
    "feishu_approval_tasks": {},
    "feishu_approval_initiated": {},
    "feishu_approval_instance": {"instance_code": "ins1"},
    "feishu_approval_approve": {"instance_code": "ins1", "task_id": "task1"},
    "feishu_approval_reject": {"instance_code": "ins1", "task_id": "task1", "comment": "no"},
    "feishu_okr_cycles": {},
    "feishu_okr_objectives": {"cycle_id": "c1"},
    "feishu_okr_key_results": {"objective_id": "o1"},
    "feishu_attendance": {"start_date": "2026-09-01", "end_date": "2026-09-19"},
}
SENDING = {
    "feishu_send_message",
    "feishu_reply_message",
    "feishu_forward_message",
    "feishu_mail_send",
}


def _body(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _upstream(method, path):
    if path.endswith("/calendars/primary"):
        return {"calendars": [{"calendar": {"calendar_id": "cal_primary"}}]}
    if path.endswith("/profile"):
        return {"primary_email_address": "me@example.com"}
    if path.endswith("/drafts"):
        return {"draft_id": "d1"}
    if method == "POST" and path.endswith("/events"):
        return {"event": {"event_id": "ev_new"}}
    if path.endswith("/messages") and path.startswith("/mail/"):
        return {"items": ["m1", {"message_id": "m2"}], "has_more": True, "page_token": "p"}
    if "/mail/" in path and method == "GET":
        return {
            "message": {"subject": "Hi", "body_html": "<b>x</b>", "body_plain_text": _body("hello")}
        }
    return {"items": []}


@pytest.fixture
def upstream(monkeypatch):
    async def call(session, cipher, scope, method, path, **kwargs):
        return _upstream(method, path)

    mock = AsyncMock(side_effect=call)
    monkeypatch.setattr(service, "api_request", mock)
    # Mail dedup records, in memory; the database version is covered by the API tests.
    records = {}

    async def claim(session, scope, key, fingerprint):
        record = records.setdefault(
            key,
            SimpleNamespace(fingerprint=fingerprint, status="drafted", draft_id=None, result=None),
        )
        if record.fingerprint != fingerprint:
            raise service.PersonalError("uuid_reused")
        return record

    monkeypatch.setattr(sends, "claim", claim)
    return mock


async def invoke(name, args):
    scope = SimpleNamespace(open_id="ou_self", platform_user_id="u_self", scheduled=False)
    return await tools.dispatch(SimpleNamespace(flush=AsyncMock()), None, scope, name, args)


def calls(mock):
    return [(c.args[3], c.args[4], c.kwargs) for c in mock.call_args_list]


def test_every_tool_is_sampled_and_every_permission_is_requested_at_creation():
    assert set(SAMPLES) == set(tools.TOOLS)
    requested = set(manifest.USER_SCOPES)
    for endpoint in endpoints.ENDPOINTS.values():
        assert endpoint.scopes[0] in requested and endpoint.also <= requested
    assert {"calendar:calendar.event:create", "mail:user_mailbox.message:send"} <= requested
    assert {"task:task:write", "bitable:app", "approval:task:write"} <= requested


@pytest.mark.parametrize("name", sorted(SAMPLES))
async def test_every_tool_calls_only_registered_endpoints(upstream, name):
    out = await invoke(name, SAMPLES[name])
    assert "error" not in out, out
    assert out["content_trust"] == "external_untrusted_data"
    assert upstream.call_args_list
    for method, path, _ in calls(upstream):
        assert endpoints.match(method, path) is not None, (method, path)


def _names(level, scopes):
    return {item["name"] for item in tools.definitions(auth=False, level=level, scopes=scopes)}


def test_listing_follows_the_saved_tier_and_granted_permissions():
    everything = endpoints.manifest_scopes()
    assert _names("messages_readonly", permissions.MESSAGE_SCOPES) == {
        "feishu_search_messages",
        "feishu_read_messages",
        "feishu_chat_history",
        "feishu_list_chats",
    }
    middle = _names("all_except_send", everything)
    assert not middle & SENDING
    assert {"feishu_calendar_create_event", "feishu_mail_create_draft"} <= middle
    assert _names("all", everything) == set(tools.TOOLS)
    calendar_read = _names("all", {"calendar:calendar.event:read", "calendar:calendar:read"})
    assert "feishu_calendar_events" in calendar_read
    assert "feishu_calendar_create_event" not in calendar_read
    assert not {name for name in calendar_read if name.startswith("feishu_mail")}
    # Sending as the user needs both message permissions.
    assert "feishu_send_message" not in _names("all", {"im:message.send_as_user"})


async def test_events_default_to_the_primary_calendar_in_seconds(upstream):
    await invoke("feishu_calendar_events", SAMPLES["feishu_calendar_events"])
    (m1, p1, _), (m2, p2, kw) = calls(upstream)
    assert (m1, p1) == ("POST", "/calendar/v4/calendars/primary")
    assert p2 == "/calendar/v4/calendars/cal_primary/events/instance_view"
    assert kw["params"]["start_time"] == "1789747200"
    upstream.reset_mock()
    await invoke(
        "feishu_calendar_events",
        {
            **SAMPLES["feishu_calendar_events"],
            "calendar_id": "feishu.cn_x@group.calendar.feishu.cn",
        },
    )
    assert [p for _, p, _ in calls(upstream)] == [
        "/calendar/v4/calendars/feishu.cn_x@group.calendar.feishu.cn/events/instance_view"
    ]


async def test_created_event_invites_attendees_and_reports_a_failed_invite(upstream):
    args = {**SAMPLES["feishu_calendar_create_event"], "attendee_open_ids": ["ou_1", "ou_2"]}
    out = await invoke("feishu_calendar_create_event", args)
    create, invite = calls(upstream)[1:]
    assert create[2]["json"]["start_time"] == {"timestamp": "1789783200"}
    assert create[2]["params"]["idempotency_key"] == UUID
    assert invite[1] == "/calendar/v4/calendars/cal_primary/events/ev_new/attendees"
    assert invite[2]["json"]["attendees"][1] == {"type": "user", "user_id": "ou_2"}
    assert out["event"] == {"event_id": "ev_new"}

    async def refuse_invite(session, cipher, scope, method, path, **kwargs):
        if path.endswith("/attendees"):
            raise service.PersonalError("feishu_request_failed", upstream_code=190002)
        return _upstream(method, path)

    upstream.side_effect = refuse_invite
    out = await invoke("feishu_calendar_create_event", args)
    assert out["event"] == {"event_id": "ev_new"}
    assert out["attendees_error"] == {"error": "feishu_request_failed", "upstream_code": 190002}


async def test_mail_is_sent_from_the_owner_mailbox_as_a_draft_first(upstream):
    out = await invoke(
        "feishu_mail_send",
        {
            "to": ["a@example.com"],
            "cc": ["b@example.com"],
            "subject": "周报",
            "body": "内容",
            "uuid": UUID,
        },
    )
    (_, profile, _), (_, draft, kw), (_, send, _) = calls(upstream)
    assert profile == "/mail/v1/user_mailboxes/me/profile"
    assert draft == "/mail/v1/user_mailboxes/me/drafts"
    assert send == "/mail/v1/user_mailboxes/me/drafts/d1/send"
    raw = base64.urlsafe_b64decode(kw["json"]["raw"])
    message = email.message_from_bytes(raw, policy=policy.default)
    assert (message["From"], message["To"], message["Cc"]) == (
        "me@example.com",
        "a@example.com",
        "b@example.com",
    )
    assert message["Subject"] == "周报" and message.get_content().strip() == "内容"
    assert out["sent"] is True and out["draft_id"] == "d1"


async def test_mail_retry_with_the_same_uuid_never_sends_twice(upstream):
    args = SAMPLES["feishu_mail_send"]

    async def send_fails_once(session, cipher, scope, method, path, **kwargs):
        if path.endswith("/send") and not getattr(send_fails_once, "failed", False):
            send_fails_once.failed = True
            raise service.PersonalError("feishu_request_failed", upstream_code=500)
        return _upstream(method, path)

    upstream.side_effect = send_fails_once
    with pytest.raises(service.PersonalError, match="feishu_request_failed"):
        await invoke("feishu_mail_send", args)
    # The retry sends the draft already made instead of composing a new mail.
    assert (await invoke("feishu_mail_send", args))["sent"] is True
    repeat = await invoke("feishu_mail_send", args)
    assert repeat["sent"] is True and repeat["repeat"] is True
    paths = [path for _, path, _ in calls(upstream)]
    assert paths.count("/mail/v1/user_mailboxes/me/drafts") == 1
    assert paths.count("/mail/v1/user_mailboxes/me/drafts/d1/send") == 2
    with pytest.raises(service.PersonalError, match="uuid_reused"):
        await invoke("feishu_mail_send", {**args, "subject": "Another"})


async def test_mail_resend_that_fails_is_unconfirmed_not_retried(upstream):
    args = {**SAMPLES["feishu_mail_send"], "uuid": "lost-reply"}

    async def reply_lost(session, cipher, scope, method, path, **kwargs):
        if path.endswith("/send"):
            if not getattr(reply_lost, "tried", False):
                reply_lost.tried = True
                raise service.PersonalError("upstream_unavailable")
            # Feishu sent it the first time, so the draft is gone.
            raise service.PersonalError("feishu_request_failed", upstream_code=1230001)
        return _upstream(method, path)

    upstream.side_effect = reply_lost
    with pytest.raises(service.PersonalError, match="upstream_unavailable"):
        await invoke("feishu_mail_send", args)
    with pytest.raises(service.PersonalError) as caught:
        await invoke("feishu_mail_send", args)
    assert caught.value.payload() == {"error": "mail_send_unconfirmed", "upstream_code": 1230001}
    assert [p for _, p, _ in calls(upstream)].count("/mail/v1/user_mailboxes/me/drafts") == 1


async def test_mail_read_decodes_the_body_and_list_fetches_summaries(upstream):
    out = await invoke("feishu_mail_read", {"message_id": "TWFpbA==", "text_limit": 3})
    assert out["message"]["body_plain_text"] == "hel" and "body_html" not in out["message"]
    assert out["next_offset"] == 3
    upstream.reset_mock()
    out = await invoke("feishu_mail_list", {"only_unread": True})
    (_, _, listed), (_, batch, got) = calls(upstream)
    assert listed["params"] == {"page_size": 15, "folder_id": "INBOX", "only_unread": "true"}
    assert batch.endswith("/messages/batch_get")
    assert got["json"] == {"message_ids": ["m1", "m2"], "format": "metadata"}
    assert out["has_more"] is True and out["page_token"] == "p"


async def test_task_completion_sets_only_the_changed_fields(upstream):
    await invoke("feishu_task_update", {"task_guid": "t-1", "completed": True})
    body = calls(upstream)[0][2]["json"]
    assert body["update_fields"] == ["completed_at"] and body["task"]["completed_at"] != "0"
    upstream.reset_mock()
    await invoke(
        "feishu_task_create",
        {
            "summary": "Report",
            "due": "2026-09-30",
            "assignee_open_ids": ["ou_1"],
            "client_token": UUID,
        },
    )
    body = calls(upstream)[0][2]["json"]
    assert body["due"] == {"timestamp": "1790726400000", "is_all_day": True}
    assert body["members"] == [{"id": "ou_1", "type": "user", "role": "assignee"}]


async def test_own_identity_is_used_where_the_api_names_a_person(upstream):
    await invoke("feishu_attendance", SAMPLES["feishu_attendance"])
    body = calls(upstream)[0][2]["json"]
    assert body == {"user_ids": ["u_self"], "check_date_from": 20260901, "check_date_to": 20260919}
    upstream.reset_mock()
    await invoke("feishu_okr_cycles", {})
    assert calls(upstream)[0][2]["params"]["user_id"] == "ou_self"
    upstream.reset_mock()
    await invoke("feishu_calendar_freebusy", SAMPLES["feishu_calendar_freebusy"])
    assert calls(upstream)[0][2]["json"]["user_id"] == "ou_self"


@pytest.mark.parametrize(
    "name,args",
    [
        ("feishu_calendar_event", {"calendar_id": "..", "event_id": "ev_0"}),
        ("feishu_calendar_event", {"event_id": "../../im/v1/messages"}),
        ("feishu_mail_read", {"message_id": "a/b"}),
        (
            "feishu_mail_send",
            {
                "to": ["a@example.com"],
                "subject": "x\r\nBcc: c@example.com",
                "body": "b",
                "uuid": UUID,
            },
        ),
        (
            "feishu_mail_send",
            {"to": ["not an address"], "subject": "x", "body": "b", "uuid": UUID},
        ),
        ("feishu_mail_send", {"to": ["a@example.com"], "subject": "x", "body": "b"}),
        (
            "feishu_calendar_events",
            {"start_time": "2026-09-01T00:00:00Z", "end_time": "2026-11-01T00:00:00Z"},
        ),
        ("feishu_calendar_update_event", {"event_id": "ev_0"}),
        ("feishu_task_update", {"task_guid": "t-1"}),
        ("feishu_attendance", {"start_date": "2026-08-01", "end_date": "2026-09-19"}),
        ("feishu_sheet_read", {"spreadsheet_token": "sht1", "ranges": ["s1!A1:B2&x=1"]}),
        ("feishu_approval_tasks", {"topic": "all"}),
    ],
)
async def test_unsafe_or_incomplete_arguments_stop_before_http(upstream, name, args):
    assert (await invoke(name, args))["error"] == "invalid_tool_or_arguments"
    upstream.assert_not_called()


def test_only_registered_paths_match():
    assert endpoints.match("GET", "/wiki/v2/spaces/get_node") is endpoints.ENDPOINTS["wiki.node"]
    assert endpoints.match("GET", "/wiki/v2/spaces/sp1/nodes") is endpoints.ENDPOINTS["wiki.nodes"]
    for method, path in [
        ("GET", "/docx/v1/documents/../raw_content"),
        ("GET", "/docx/v1/documents/a/b/raw_content"),
        ("GET", "/contact/v3/users"),
        ("POST", "/im/v1/messages/om_1/urgent_app"),
        ("DELETE", "/task/v2/tasklists/tl-1"),
        ("GET", "/im/v1/messages?container_id=x"),
    ]:
        assert endpoints.match(method, path) is None, path
    with pytest.raises(ValueError):
        endpoints.build(endpoints.ENDPOINTS["docx.raw"], document_id="..")


@pytest.mark.parametrize(
    "key,level,scopes,reason",
    [
        (
            "im.send",
            "all_except_send",
            {"im:message", "im:message.send_as_user"},
            "sending_not_authorized",
        ),
        ("im.send", "all", {"im:message.send_as_user"}, "sending_not_authorized"),
        ("im.send", "all", {"im:message", "im:message.send_as_user"}, None),
        ("mail.send_draft", "all", {"mail:user_mailbox.message:send"}, None),
        (
            "calendar.create",
            "messages_readonly",
            {"calendar:calendar.event:create"},
            "outside_selected_authorization",
        ),
        ("calendar.create", "all_except_send", {"calendar:calendar.event:create"}, None),
        ("calendar.create", "all_except_send", set(), "selected_permission_missing"),
        ("calendar.reply", "all_except_send", {"calendar:calendar.event:reply"}, None),
        (
            "calendar.event",
            "messages_readonly",
            {"calendar:calendar.event:read"},
            "outside_selected_authorization",
        ),
        ("im.chats", "messages_readonly", {"im:chat:read"}, None),
        ("docx.raw", "legacy_readonly", set(), None),
        ("calendar.event", "legacy_readonly", set(), "outside_selected_authorization"),
        ("task.create", "legacy_readonly", {"task:task:write"}, "outside_selected_authorization"),
        ("task.create", "unknown", {"task:task:write"}, "outside_selected_authorization"),
    ],
)
def test_denied_follows_tier_kind_and_permissions(key, level, scopes, reason):
    assert endpoints.denied(endpoints.ENDPOINTS[key], level, frozenset(scopes)) == reason

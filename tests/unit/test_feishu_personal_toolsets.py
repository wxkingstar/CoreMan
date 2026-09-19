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
    "feishu_meeting_rooms": {"keyword": "5F"},
    "feishu_task_add_subtask": {
        "parent_task_guid": "t-1",
        "summary": "Draft",
        "client_token": UUID,
    },
    "feishu_edit_document": {
        "document_id": "doc1",
        "command": "str_replace",
        "pattern": "old",
        "content": "new",
    },
    "feishu_share_document": {"token": "doc1", "doc_type": "docx", "member_open_ids": ["ou_1"]},
    "feishu_create_folder": {"name": "Reports"},
    "feishu_approval_transfer": {
        "instance_code": "ins1",
        "task_id": "task1",
        "transfer_open_id": "ou_2",
    },
    "feishu_approval_templates": {"keyword": "请假"},
    "feishu_approval_template": {"approval_code": "LEAVE-1"},
    "feishu_approval_submit": {
        "approval_code": "LEAVE-1",
        "form": [{"id": "widget1", "type": "input", "value": "年假"}],
        "uuid": UUID,
    },
    "feishu_approval_recall": {"instance_code": "ins1"},
    "feishu_approval_remind": {"instance_code": "ins1", "task_ids": ["task1"]},
    "feishu_message_read_users": {"message_id": "om_1"},
    "feishu_chat_announcement": {"chat_id": "oc_1"},
    "feishu_edit_message": {"message_id": "om_1", "text": "fixed"},
    "feishu_task_search": {"query": "report"},
    "feishu_file_info": {"files": [{"token": "doc1", "type": "docx"}]},
    "feishu_move_file": {"token": "doc1", "type": "docx", "folder_token": "fld1"},
    "feishu_copy_file": {"token": "doc1", "type": "docx", "name": "Copy", "folder_token": "fld1"},
    "feishu_rename_file": {"token": "doc1", "type": "docx", "new_title": "New"},
    "feishu_sheet_manage": {"spreadsheet_token": "sht1", "action": "add", "title": "Q3"},
    "feishu_base_create_table": {"app_token": "app1", "name": "Leads"},
    "feishu_base_create_field": {"app_token": "app1", "table_id": "tbl1", "name": "Owner"},
    "feishu_search_departments": {"query": "市场"},
    "feishu_get_department": {"department_id": "od-1"},
    "feishu_department_members": {"department_id": "od-1"},
    "feishu_okr_add_progress": {
        "target_type": "key_result",
        "target_id": "kr1",
        "text": "完成 60%",
        "percent": 60,
    },
    "feishu_okr_update_progress": {"progress_id": "p1", "text": "完成 80%"},
    "feishu_download_mail_attachment": {"message_id": "m1", "attachment_id": "att1"},
    "feishu_download_message_file": {"message_id": "om_1", "file_key": "fk_1"},
    "feishu_download_drive_file": {"file_token": "box1"},
    "feishu_prepare_upload": {"filename": "plan.pdf"},
    "feishu_send_file": {
        "receive_id": "oc_1",
        "receive_id_type": "chat_id",
        "upload_id": "upload-0123456789abcdef",
        "uuid": UUID,
    },
    "feishu_save_to_drive": {"upload_id": "upload-0123456789abcdef"},
}
# Tools that only hand out an upload link and never call Feishu.
OFFLINE = {"feishu_prepare_upload"}
UPLOAD = "upload-0123456789abcdef"
SENDING = {
    "feishu_send_message",
    "feishu_reply_message",
    "feishu_forward_message",
    "feishu_mail_send",
}


def _body(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _upstream(method, path, params=None):
    if path.endswith("/attachments/download_url"):
        ids = (params or {}).get("attachment_ids") or []
        return {
            "download_urls": [
                {"attachment_id": i, "download_url": f"https://files.feishu.cn/att/{i}"}
                for i in ids
            ]
        }
    if path == "/im/v1/files":
        return {"file_key": "file_1"}
    if path == "/im/v1/images":
        return {"image_key": "img_1"}
    if path == "/drive/explorer/v2/root_folder/meta":
        return {"token": "root1"}
    if path == "/drive/v1/files/upload_all":
        return {"file_token": "box9"}
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


class FakeDownload:
    def __init__(self, data=b"file-bytes", filename="a.pdf", content_type="application/pdf"):
        self.data, self.filename, self.content_type = data, filename, content_type

    async def chunks(self, limit):
        if len(self.data) > limit:
            raise service.PersonalError("file_too_large")
        yield self.data

    async def aclose(self):
        pass


class FakeRelay:
    """The object store and sealed links, in memory; the real ones are covered by API tests."""

    def __init__(self):
        self.saved = []
        self.uploads = {UPLOAD: ("report.pdf", "application/pdf", b"%PDF-1.4 demo")}

    async def save(self, session, source, *, filename, mime):
        data = b"".join([chunk async for chunk in source])
        row = SimpleNamespace(
            id=len(self.saved), filename=filename, content_type=mime, size=len(data), data=data
        )
        self.saved.append(row)
        return row

    def download_link(self, grant, row):
        return f"https://coreman.example.com/api/runtime/feishu-personal/files/t{row.id}"

    def upload_link(self, grant, filename, mime):
        return "https://coreman.example.com/api/runtime/feishu-personal/uploads/t"

    async def uploaded(self, session, scope, upload_id):
        if upload_id not in self.uploads:
            raise service.PersonalError("upload_not_found")
        name, mime, data = self.uploads[upload_id]
        return SimpleNamespace(filename=name, content_type=mime, size=len(data), data=data)

    async def read(self, row):
        return row.data


RELAY = {}


@pytest.fixture
def upstream(monkeypatch):
    async def call(session, cipher, scope, method, path, **kwargs):
        return _upstream(method, path, kwargs.get("params"))

    mock = AsyncMock(side_effect=call)
    monkeypatch.setattr(service, "api_request", mock)

    async def download(session, cipher, scope, method, path, **kwargs):
        await mock(session, cipher, scope, method, path, **kwargs)
        return mock.next_download or FakeDownload()

    mock.next_download = None
    mock.signed = []

    async def signed(url):
        mock.signed.append(url)
        return FakeDownload(b"original attachment", "budget.xlsx", "application/vnd.ms-excel")

    monkeypatch.setattr(service, "api_download", download)
    monkeypatch.setattr(service, "signed_download", signed)
    grant = SimpleNamespace(status="connected", token_enc=b"x")
    monkeypatch.setattr(service, "existing_row", AsyncMock(return_value=grant))
    RELAY["current"] = mock.relay = FakeRelay()
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
    session = SimpleNamespace(flush=AsyncMock())
    return await tools.dispatch(session, None, scope, name, args, RELAY.get("current"))


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
    assert bool(upstream.call_args_list) is (name not in OFFLINE)
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
        "feishu_message_read_users",
        "feishu_download_message_file",
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


# An app like the one that hit Feishu's limit: everything CoreMan asks for, older broad
# permissions, and hundreds of unrelated ones (638 in production; Feishu refused them all).
CROWDED = sorted(
    set(manifest.USER_SCOPES)
    | {"calendar:calendar", "drive:drive", "docx:document", "im:chat", "wiki:wiki"}
    | {f"aily:thing{i}:read" for i in range(600)}
    | {"offline_access"}
)


@pytest.mark.parametrize("level", ["all", "all_except_send"])
def test_top_tiers_request_only_what_the_tools_use(level):
    narrowed = set(permissions.select_scopes(level, CROWDED))
    requested = endpoints.request_scopes(level, narrowed)
    assert len(requested) <= len(endpoints.ENDPOINTS)
    assert not [s for s in requested if s.startswith("aily:")]
    # The most specific permission is chosen over the broad one when the app has both.
    assert "calendar:calendar.event:read" in requested and "calendar:calendar" not in requested
    assert set(permissions.MESSAGE_SCOPES) - {"auth:user.id:read"} <= set(requested)
    granted = frozenset(requested)
    for key, endpoint in endpoints.ENDPOINTS.items():
        if level in endpoints.LEVELS_BY_KIND[endpoint.kind]:
            assert endpoints.denied(endpoint, level, granted) is None, key
    sending = {"im:message.send_as_user", "im:message", "mail:user_mailbox.message:send"}
    assert (sending <= granted) == (level == "all")


def test_broad_permissions_are_requested_when_they_are_all_the_app_has():
    requested = endpoints.request_scopes(
        "all_except_send", {"calendar:calendar", "wiki:wiki", "offline_access"}
    )
    assert requested == ["calendar:calendar", "offline_access", "wiki:wiki"]


ORIGINAL = {
    "message_id": "TWFpbA==",
    "subject": "Re: 季度预算",
    "head_from": {"mail_address": "boss@example.com", "name": "老板"},
    "to": [{"mail_address": "me@example.com"}, {"mail_address": "peer@example.com"}],
    "cc": [{"mail_address": "fin@example.com"}],
    "smtp_message_id": "<abc@example.com>",
    "references": ["<root@example.com>"],
    "internal_date": "1790000000000",
    "body_plain_text": _body("请看附件\nSubject: evil\r\nBcc: x@evil.com"),
    "attachments": [
        {"id": "a1", "filename": "budget.xlsx", "attachment_type": 1},
        {"id": "img1", "filename": "logo.png", "is_inline": True, "cid": "logo"},
        {"id": "big1", "filename": "video.mp4", "attachment_type": 2},
    ],
}


def _with_original(method, path, params=None):
    if "/messages/" in path and method == "GET" and not path.endswith("/download_url"):
        return {"message": ORIGINAL}
    return _upstream(method, path, params)


def _eml(mock):
    raw = next(kw["json"]["raw"] for _, path, kw in calls(mock) if path.endswith("/drafts"))
    return email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)


async def test_reply_all_threads_quotes_and_addresses_from_the_original(upstream):
    async def respond(session, cipher, scope, method, path, **kwargs):
        return _with_original(method, path)

    upstream.side_effect = respond
    out = await invoke(
        "feishu_mail_send",
        {"reply_to_message_id": "TWFpbA==", "reply_all": True, "body": "收到", "uuid": "r1"},
    )
    message = _eml(upstream)
    assert message["To"] == "boss@example.com"
    # Everyone else on the thread, never the user themself.
    assert message["Cc"] == "peer@example.com, fin@example.com"
    assert message["Subject"] == "回复：季度预算"
    assert message["In-Reply-To"] == "<abc@example.com>"
    assert message["References"] == "<root@example.com> <abc@example.com>"
    assert message["X-LMS-Reply-To-Message-Id"] == "TWFpbA=="
    assert message["Bcc"] is None
    text = message.get_content()
    assert text.startswith("收到") and "老板 <boss@example.com> 写道" in text
    assert "> 请看附件" in text
    assert out["sent"] is True and out["to"] == ["boss@example.com"]


async def test_forward_carries_the_original_attachments(upstream):
    async def respond(session, cipher, scope, method, path, **kwargs):
        return _with_original(method, path, kwargs.get("params"))

    upstream.side_effect = respond
    out = await invoke(
        "feishu_mail_create_draft",
        {"forward_message_id": "TWFpbA==", "to": ["new@example.com"], "body": "供参考"},
    )
    message = _eml(upstream)
    assert message["Subject"] == "转发：季度预算" and message["In-Reply-To"] is None
    assert "---------- 转发的邮件 ----------" in message.get_body(("plain",)).get_content()
    parts = list(message.iter_attachments())
    assert [(p.get_filename(), p.get_content()) for p in parts] == [
        ("budget.xlsx", b"original attachment")
    ]
    assert upstream.signed == ["https://files.feishu.cn/att/a1"]
    # A large attachment is a link Feishu keeps elsewhere; say so instead of dropping it.
    assert out == {
        **out,
        "sent": False,
        "attachments": ["budget.xlsx"],
        "attachments_not_forwarded": ["video.mp4"],
    }
    assert not [p for _, p, _ in calls(upstream) if p.endswith("/send")]


@pytest.mark.parametrize(
    "args",
    [
        {"reply_to_message_id": "m1", "forward_message_id": "m2", "body": "x", "uuid": "a"},
        {"reply_all": True, "to": ["a@example.com"], "subject": "s", "body": "x", "uuid": "a"},
        {"to": ["a@example.com"], "body": "x", "uuid": "a"},
        {"forward_message_id": "m2", "body": "x", "uuid": "a"},
    ],
)
async def test_inconsistent_mail_arguments_are_rejected(upstream, args):
    assert (await invoke("feishu_mail_send", args))["error"] == "invalid_tool_or_arguments"
    upstream.assert_not_called()


async def test_all_day_recurring_event_with_reminders_and_rooms(upstream):
    await invoke(
        "feishu_calendar_create_event",
        {
            "summary": "周会",
            "start_time": "2026-09-21",
            "end_time": "2026-09-21",
            "recurrence": "FREQ=WEEKLY;BYDAY=MO",
            "reminder_minutes": [15],
            "room_ids": ["omm_1"],
            "idempotency_key": UUID,
        },
    )
    (_, _, _), (_, _, create), (_, rooms_path, rooms) = calls(upstream)
    body = create["json"]
    # Feishu all-day events end on the next day; the user gave the last day.
    assert body["start_time"] == {"date": "2026-09-21"}
    assert body["end_time"] == {"date": "2026-09-22"}
    assert body["recurrence"] == "FREQ=WEEKLY;BYDAY=MO"
    assert body["reminders"] == [{"minutes": 15}]
    assert rooms_path.endswith("/attendees")
    assert rooms["json"]["attendees"] == [{"type": "resource", "room_id": "omm_1"}]


@pytest.mark.parametrize(
    "args",
    [
        {"start_time": "2026-09-21", "end_time": "2026-09-21T10:00:00+08:00"},
        {"start_time": "2026-09-22", "end_time": "2026-09-21"},
        {"recurrence": "RRULE:FREQ=DAILY"},
    ],
)
async def test_bad_event_times_or_rules_are_rejected(upstream, args):
    base = {
        "summary": "x",
        "start_time": "2026-09-21T09:00:00+08:00",
        "end_time": "2026-09-21T10:00:00+08:00",
        "idempotency_key": UUID,
    }
    out = await invoke("feishu_calendar_create_event", {**base, **args})
    assert out["error"] == "invalid_tool_or_arguments"
    upstream.assert_not_called()


async def test_room_search_and_room_busy_times(upstream):
    await invoke("feishu_meeting_rooms", {"keyword": "5F", "limit": 5})
    await invoke(
        "feishu_calendar_freebusy",
        {**SAMPLES["feishu_calendar_freebusy"], "room_id": "omm_1"},
    )
    (_, rooms, search), (_, _, busy) = calls(upstream)
    assert rooms == "/vc/v1/rooms/search"
    assert search["json"] == {"keyword": "5F", "page_size": 5, "search_level_name": True}
    assert busy["json"]["room_id"] == "omm_1" and "user_id" not in busy["json"]


async def test_markdown_messages_are_sent_as_rich_text(upstream):
    await invoke("feishu_send_message", {**SAMPLES["feishu_send_message"], "format": "markdown"})
    body = calls(upstream)[0][2]["json"]
    assert body["msg_type"] == "post"
    assert '"tag": "md"' in body["content"]


async def test_subtask_with_due_reminder(upstream):
    await invoke(
        "feishu_task_add_subtask",
        {
            **SAMPLES["feishu_task_add_subtask"],
            "due": "2026-09-30T18:00:00+08:00",
            "remind_minutes_before": 30,
        },
    )
    _, path, kw = calls(upstream)[0]
    assert path == "/task/v2/tasks/t-1/subtasks"
    assert kw["json"]["reminders"] == [{"relative_fire_minute": 30}]
    missing_due = {**SAMPLES["feishu_task_add_subtask"], "remind_minutes_before": 30}
    assert (await invoke("feishu_task_add_subtask", missing_due))["error"]


async def test_documents_are_read_as_markdown_and_edited_by_command(upstream):
    async def respond(session, cipher, scope, method, path, **kwargs):
        if path.endswith("/fetch"):
            return {"document": {"document_id": "doc1", "content": "# 标题\n正文"}}
        return _upstream(method, path)

    upstream.side_effect = respond
    out = await invoke("feishu_read_document", {"document_id": "wikcn1", "with_block_ids": True})
    _, path, kw = calls(upstream)[0]
    assert path == "/docs_ai/v1/documents/wikcn1/fetch"
    assert kw["json"]["export_option"]["export_block_id"] is True
    assert out["content"] == "# 标题\n正文" and out["document_id"] == "doc1"
    upstream.reset_mock()
    await invoke("feishu_read_document", {"document_id": "doc1", "format": "text"})
    assert calls(upstream)[0][1] == "/docx/v1/documents/doc1/raw_content"
    upstream.reset_mock()
    await invoke(
        "feishu_edit_document",
        {
            "document_id": "doc1",
            "command": "block_insert_after",
            "block_id": "-1",
            "content": "- a",
        },
    )
    method, path, kw = calls(upstream)[0]
    assert (method, path) == ("PUT", "/docs_ai/v1/documents/doc1")
    assert kw["json"] == {
        "format": "markdown",
        "command": "block_insert_after",
        "revision_id": -1,
        "content": "- a",
        "block_id": "-1",
    }
    for bad in (
        {"command": "str_replace", "content": "x"},
        {"command": "block_delete"},
        {"command": "overwrite"},
    ):
        out = await invoke("feishu_edit_document", {"document_id": "doc1", **bad})
        assert out["error"] == "invalid_tool_or_arguments"


async def test_sharing_reports_each_colleague(upstream):
    async def one_fails(session, cipher, scope, method, path, **kwargs):
        if kwargs["json"]["member_id"] == "ou_2":
            raise service.PersonalError("feishu_request_failed", upstream_code=1063001)
        return {}

    upstream.side_effect = one_fails
    out = await invoke(
        "feishu_share_document",
        {
            **SAMPLES["feishu_share_document"],
            "member_open_ids": ["ou_1", "ou_2"],
            "permission": "edit",
        },
    )
    assert out["shared"] == ["ou_1"]
    assert out["failed"] == [
        {"open_id": "ou_2", "error": "feishu_request_failed", "upstream_code": 1063001}
    ]
    first = calls(upstream)[0][2]
    assert first["params"] == {"type": "docx", "need_notification": "true"}
    assert first["json"] == {
        "member_type": "openid",
        "member_id": "ou_1",
        "perm": "edit",
        "type": "user",
    }


async def test_approval_form_is_sent_as_the_json_string_feishu_expects(upstream):
    await invoke(
        "feishu_approval_submit",
        {
            **SAMPLES["feishu_approval_submit"],
            "approvers": [{"key": "manager_node", "open_ids": ["ou_3"]}],
        },
    )
    body = calls(upstream)[0][2]["json"]
    assert body["form"] == '[{"id": "widget1", "type": "input", "value": "年假"}]'
    assert body["node_approver_list"] == [{"key": "manager_node", "value": ["ou_3"]}]
    assert "node_cc_list" not in body and body["uuid"] == UUID


def test_new_daily_tools_follow_the_tiers():
    everything = endpoints.manifest_scopes()
    middle = _names("all_except_send", everything)
    assert {"feishu_approval_submit", "feishu_edit_document", "feishu_meeting_rooms"} <= middle
    # Granting colleagues access notifies them, like sending.
    assert "feishu_share_document" not in middle
    assert "feishu_share_document" in _names("all", everything)


@pytest.mark.parametrize(
    "args,path",
    [
        ({"document_id": "doccn1", "kind": "doc"}, "/doc/v2/doccn1/raw_content"),
        ({"document_id": "sld1", "kind": "slides"}, "/slides_ai/v1/xml_presentations/sld1"),
        ({"document_id": "mnd1", "kind": "mindnote"}, "/mindnote/v1/mindnotes/mnd1/nodes"),
    ],
)
async def test_other_document_kinds_are_read_through_their_own_apis(upstream, args, path):
    await invoke("feishu_read_document", args)
    assert calls(upstream)[0][1] == path


async def test_related_tasks_and_search_filters(upstream):
    await invoke("feishu_task_list", {"relation": "related", "completed": False})
    _, path, kw = calls(upstream)[0]
    assert path == "/task/v2/task_v2/list_related_task" and kw["params"]["completed"] == "false"
    upstream.reset_mock()
    await invoke(
        "feishu_task_search",
        {"creator_open_ids": ["ou_self"], "due_before": "2026-09-30T00:00:00+08:00"},
    )
    body = calls(upstream)[0][2]["json"]
    assert body["filter"] == {
        "creator_ids": ["ou_self"],
        "due_time": {"end_time": "2026-09-30T00:00:00+08:00"},
    }
    assert (await invoke("feishu_task_search", {}))["error"] == "invalid_tool_or_arguments"


async def test_okr_progress_uses_feishus_rich_text_and_status_codes(upstream):
    await invoke(
        "feishu_okr_add_progress", {**SAMPLES["feishu_okr_add_progress"], "status": "overdue"}
    )
    _, path, kw = calls(upstream)[0]
    body = kw["json"]
    assert path == "/okr/v1/progress_records/"
    assert body["target_type"] == 3 and body["progress_rate"] == {"percent": 60, "status": 1}
    text = body["content"]["blocks"][0]["paragraph"]["elements"][0]
    assert text == {"type": "textRun", "textRun": {"text": "完成 60%"}}


async def test_sheet_and_base_structure_requests(upstream):
    await invoke(
        "feishu_sheet_manage",
        {"spreadsheet_token": "sht1", "action": "rename", "title": "Q4", "sheet_id": "s1"},
    )
    await invoke(
        "feishu_base_create_table",
        {
            "app_token": "app1",
            "name": "Leads",
            "fields": [{"name": "Stage", "type": "single_select", "options": ["New", "Won"]}],
        },
    )
    (_, _, sheet), (_, _, table) = calls(upstream)
    assert sheet["json"] == {
        "requests": [{"updateSheet": {"properties": {"sheetId": "s1", "title": "Q4"}}}]
    }
    field = table["json"]["table"]["fields"][0]
    assert field == {
        "field_name": "Stage",
        "type": 3,
        "property": {"options": [{"name": "New"}, {"name": "Won"}]},
    }
    bad = {"spreadsheet_token": "sht1", "action": "rename", "title": "x"}
    assert (await invoke("feishu_sheet_manage", bad))["error"] == "invalid_tool_or_arguments"


def test_editing_a_sent_message_is_top_tier_like_sending():
    everything = endpoints.manifest_scopes()
    assert "feishu_edit_message" not in _names("all_except_send", everything)
    assert "feishu_edit_message" in _names("all", everything)
    assert "feishu_message_read_users" in _names("messages_readonly", permissions.MESSAGE_SCOPES)


async def test_mail_attaches_uploaded_files(upstream):
    out = await invoke(
        "feishu_mail_send",
        {
            "to": ["a@example.com"],
            "subject": "方案",
            "body": "见附件",
            "attachment_ids": [UPLOAD],
            "uuid": "att-1",
        },
    )
    message = _eml(upstream)
    assert message.get_body(("plain",)).get_content().strip() == "见附件"
    [part] = message.iter_attachments()
    assert part.get_filename() == "report.pdf" and part.get_content() == b"%PDF-1.4 demo"
    assert out["sent"] is True and out["attachments"] == ["report.pdf"]


async def test_mail_attachments_over_the_limit_are_refused_before_any_draft(upstream, monkeypatch):
    from coreman.core.feishu_personal.toolsets import mail

    monkeypatch.setattr(mail, "MAIL_ATTACHMENT_LIMIT", 5)
    with pytest.raises(service.PersonalError) as refused:
        await invoke(
            "feishu_mail_create_draft",
            {"to": ["a@example.com"], "subject": "s", "body": "x", "attachment_ids": [UPLOAD]},
        )
    assert refused.value.code == "file_too_large"
    assert not [p for _, p, _ in calls(upstream) if p.endswith("/drafts")]


async def test_downloads_become_short_lived_links(upstream):
    out = await invoke(
        "feishu_download_mail_attachment",
        {"message_id": "m1", "attachment_id": "att1", "filename": "合同.pdf"},
    )
    assert upstream.signed == ["https://files.feishu.cn/att/att1"]
    assert out["download_url"].endswith("/files/t0") and out["filename"] == "合同.pdf"
    assert "curl" in out["how"]
    upstream.next_download = FakeDownload(b"png", None, "image/png")
    out = await invoke(
        "feishu_download_message_file", {"message_id": "om_1", "file_key": "img_1", "type": "image"}
    )
    # Images arrive without a name; the type still gives the file an extension.
    assert out["filename"] == "file.png"
    _, path, kwargs = calls(upstream)[-1]
    assert path == "/im/v1/messages/om_1/resources/img_1" and kwargs["params"] == {"type": "image"}


async def test_files_are_sent_as_images_or_typed_files(upstream):
    base = {"receive_id": "ou_1", "receive_id_type": "open_id", "upload_id": UPLOAD}
    await invoke("feishu_send_file", {**base, "uuid": "f1"})
    upload, send = calls(upstream)[-2:]
    assert upload[1] == "/im/v1/files" and upload[2]["data"]["file_type"] == "pdf"
    assert send[2]["json"]["msg_type"] == "file"
    assert send[2]["json"]["content"] == '{"file_key": "file_1"}'
    await invoke(
        "feishu_send_file",
        {**base, "as_image": True, "reply_to_message_id": "om_7", "uuid": "f2"},
    )
    upload, reply = calls(upstream)[-2:]
    assert upload[1] == "/im/v1/images" and upload[2]["data"] == {"image_type": "message"}
    assert reply[1] == "/im/v1/messages/om_7/reply" and reply[2]["json"]["msg_type"] == "image"
    with pytest.raises(service.PersonalError) as missing:
        await invoke("feishu_send_file", {**base, "upload_id": "x" * 20, "uuid": "f3"})
    assert missing.value.code == "upload_not_found"


async def test_saving_to_drive_defaults_to_my_space_root(upstream):
    out = await invoke("feishu_save_to_drive", {"upload_id": UPLOAD, "name": "归档.pdf"})
    root, saved = calls(upstream)
    assert root[1] == "/drive/explorer/v2/root_folder/meta"
    assert saved[2]["data"] == {
        "file_name": "归档.pdf",
        "parent_type": "explorer",
        "parent_node": "root1",
        "size": "13",
    }
    assert out["file_token"] == "box9" and out["folder_token"] == "root1"


def test_file_tools_follow_the_tiers():
    everything = endpoints.manifest_scopes()
    middle = _names("all_except_send", everything)
    assert {"feishu_download_drive_file", "feishu_prepare_upload", "feishu_save_to_drive"} <= middle
    assert "feishu_send_file" not in middle
    assert "feishu_send_file" in _names("all", everything)
    # Reading messages includes the files people sent in them.
    readonly = _names("messages_readonly", permissions.MESSAGE_SCOPES)
    assert {n for n in readonly if "file" in n} == {"feishu_download_message_file"}


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/a",
        "http://files.feishu.cn/a",
        "https://feishu.cn.evil.example.com/a",
        "https://user@files.feishu.cn/a",
    ],
)
async def test_presigned_downloads_only_go_to_feishu(url):
    with pytest.raises(service.PersonalError):
        await service.signed_download(url)


def test_download_names_come_from_content_disposition():
    import httpx

    def name(header):
        response = httpx.Response(200, headers={"content-disposition": header})
        return service.Download(httpx.AsyncClient(), response).filename

    assert name("attachment; filename*=UTF-8''%E6%8A%A5%E5%91%8A.pdf") == "报告.pdf"
    assert name('attachment; filename="a b.docx"') == "a b.docx"
    assert name("inline") is None

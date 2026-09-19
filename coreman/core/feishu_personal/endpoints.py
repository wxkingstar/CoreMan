"""The only Feishu open APIs personal tools may call, with the tier and permissions each needs.

Every request is matched back to this list before the owner's token leaves the server, so a
tool can never reach a path that is not registered here. `kind` decides which authorization
tier may call it: `read` for every tier, `write` for the two "all" tiers, `send` (anything that
delivers a message or mail to someone else) only for the tier that includes sending.

`scopes` lists every user permission Feishu accepts for the API; holding any one of them in
the owner's selected and actually granted scopes is enough. The first is the one the app
manifest requests. `also` must all be held in addition (sending as the user needs both).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cached_property
from typing import Literal

from coreman.core.feishu_personal.permissions import MESSAGE_SCOPES

Kind = Literal["read", "write", "send"]
LEVELS_BY_KIND: dict[str, frozenset[str]] = {
    "read": frozenset({"legacy_readonly", "messages_readonly", "all_except_send", "all"}),
    "write": frozenset({"all_except_send", "all"}),
    "send": frozenset({"all"}),
}
# Grants made before tiers existed were issued for this fixed read-only set.
LEGACY_SCOPES = frozenset(
    {
        "search:message",
        "im:message:readonly",
        "vc:meeting.search:read",
        "vc:meeting:readonly",
        "vc:note:read",
        "minutes:minutes.search:read",
        "minutes:minutes.basic:read",
        "minutes:minutes.artifacts:read",
        "docx:document:readonly",
    }
)
_PARAM = re.compile(r"\{([a-z_]+)\}")
# Path values are opaque platform IDs. No slash, query or fragment, and never "." or "..".
_SEGMENT = re.compile(r"[A-Za-z0-9_@=+-][A-Za-z0-9_.@=+-]{0,255}\Z")


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str
    kind: Kind
    scopes: tuple[str, ...]
    also: frozenset[str] = field(default_factory=frozenset)

    @cached_property
    def pattern(self) -> re.Pattern[str]:
        parts, last = [], 0
        for found in _PARAM.finditer(self.path):
            group = f"(?P<{found.group(1)}>[^/?#]+)"
            parts += [re.escape(self.path[last : found.start()]), group]
            last = found.end()
        return re.compile("".join(parts) + re.escape(self.path[last:]) + r"\Z")


def _e(method: str, path: str, kind: Kind, *scopes: str, also: tuple[str, ...] = ()) -> Endpoint:
    return Endpoint(method, path, kind, scopes, frozenset(also))


_SEND_AS_USER = ("im:message",)
_CAL_READ = ("calendar:calendar.event:read", "calendar:calendar:readonly", "calendar:calendar")
_MAILBOX = "/mail/v1/user_mailboxes/{mailbox}"
_SHEET_READ = (
    "sheets:spreadsheet:read",
    "sheets:spreadsheet:readonly",
    "sheets:spreadsheet",
    "drive:drive",
    "drive:drive:readonly",
)
_SHEET_WRITE = ("sheets:spreadsheet:write_only", "sheets:spreadsheet", "drive:drive")
_TABLE = "/bitable/v1/apps/{app_token}/tables/{table_id}"
_WIKI_READ = ("wiki:wiki:readonly", "wiki:wiki")

ENDPOINTS: dict[str, Endpoint] = {
    # 消息与群
    "im.search": _e("POST", "/im/v1/messages/search", "read", "search:message"),
    "im.mget": _e("GET", "/im/v1/messages/mget", "read", "im:message:readonly"),
    "im.history": _e("GET", "/im/v1/messages", "read", "im:message:readonly"),
    "im.send": _e("POST", "/im/v1/messages", "send", "im:message.send_as_user", also=_SEND_AS_USER),
    "im.reply": _e(
        "POST",
        "/im/v1/messages/{message_id}/reply",
        "send",
        "im:message.send_as_user",
        also=_SEND_AS_USER,
    ),
    "im.forward": _e(
        "POST",
        "/im/v1/messages/{message_id}/forward",
        "send",
        "im:message.send_as_user",
        also=_SEND_AS_USER,
    ),
    "im.recall": _e(
        "DELETE", "/im/v1/messages/{message_id}", "write", "im:message:recall", "im:message"
    ),
    "im.reaction": _e(
        "POST",
        "/im/v1/messages/{message_id}/reactions",
        "write",
        "im:message.reactions:write_only",
        "im:message",
    ),
    "im.pin": _e("POST", "/im/v1/pins", "write", "im:message.pins:write_only", "im:message"),
    "im.chats": _e("GET", "/im/v1/chats", "read", "im:chat:read", "im:chat:readonly", "im:chat"),
    "im.chat_search": _e(
        "GET", "/im/v1/chats/search", "read", "im:chat:read", "im:chat:readonly", "im:chat"
    ),
    "im.chat_members": _e(
        "GET",
        "/im/v1/chats/{chat_id}/members",
        "read",
        "im:chat.members:read",
        "im:chat:readonly",
        "im:chat.group_info:readonly",
        "im:chat",
    ),
    "im.chat_create": _e("POST", "/im/v1/chats", "write", "im:chat:create_by_user", "im:chat"),
    "im.chat_add_members": _e(
        "POST", "/im/v1/chats/{chat_id}/members", "write", "im:chat.members:write_only", "im:chat"
    ),
    # 会议、妙记
    "vc.search": _e("POST", "/vc/v1/meetings/search", "read", "vc:meeting.search:read"),
    "vc.meeting": _e(
        "GET",
        "/vc/v1/meetings/{meeting_id}",
        "read",
        "vc:meeting:readonly",
        "vc:meeting.meetingevent:read",
    ),
    "vc.note": _e("GET", "/vc/v1/notes/{note_id}", "read", "vc:note:read"),
    "minutes.search": _e(
        "POST", "/minutes/v1/minutes/search", "read", "minutes:minutes.search:read"
    ),
    "minutes.get": _e(
        "GET",
        "/minutes/v1/minutes/{minute_token}",
        "read",
        "minutes:minutes.basic:read",
        "minutes:minutes:readonly",
        "minutes:minutes",
    ),
    "minutes.artifacts": _e(
        "GET",
        "/minutes/v1/minutes/{minute_token}/artifacts",
        "read",
        "minutes:minutes.artifacts:read",
    ),
    # 日程
    "calendar.primary": _e(
        "POST",
        "/calendar/v4/calendars/primary",
        "read",
        "calendar:calendar:read",
        "calendar:calendar:readonly",
        "calendar:calendar",
    ),
    "calendar.list": _e(
        "GET",
        "/calendar/v4/calendars",
        "read",
        "calendar:calendar:read",
        "calendar:calendar:readonly",
        "calendar:calendar",
    ),
    "calendar.instances": _e(
        "GET", "/calendar/v4/calendars/{calendar_id}/events/instance_view", "read", *_CAL_READ
    ),
    "calendar.event": _e(
        "GET", "/calendar/v4/calendars/{calendar_id}/events/{event_id}", "read", *_CAL_READ
    ),
    "calendar.search": _e(
        "POST", "/calendar/v4/calendars/{calendar_id}/events/search_event", "read", *_CAL_READ
    ),
    "calendar.freebusy": _e(
        "POST",
        "/calendar/v4/freebusy/list",
        "read",
        "calendar:calendar.free_busy:read",
        "calendar:calendar:readonly",
        "calendar:calendar",
    ),
    "calendar.create": _e(
        "POST",
        "/calendar/v4/calendars/{calendar_id}/events",
        "write",
        "calendar:calendar.event:create",
        "calendar:calendar",
    ),
    "calendar.update": _e(
        "PATCH",
        "/calendar/v4/calendars/{calendar_id}/events/{event_id}",
        "write",
        "calendar:calendar.event:update",
        "calendar:calendar",
    ),
    "calendar.delete": _e(
        "DELETE",
        "/calendar/v4/calendars/{calendar_id}/events/{event_id}",
        "write",
        "calendar:calendar.event:delete",
        "calendar:calendar",
    ),
    "calendar.attendees": _e(
        "POST",
        "/calendar/v4/calendars/{calendar_id}/events/{event_id}/attendees",
        "write",
        "calendar:calendar.event:update",
        "calendar:calendar",
    ),
    "calendar.reply": _e(
        "POST",
        "/calendar/v4/calendars/{calendar_id}/events/{event_id}/reply",
        "write",
        "calendar:calendar.event:reply",
        "calendar:calendar",
    ),
    # 邮件（mailbox 固定为 me，只访问本人邮箱）
    "mail.profile": _e("GET", _MAILBOX + "/profile", "read", "mail:user_mailbox:readonly"),
    "mail.folders": _e(
        "GET",
        _MAILBOX + "/folders",
        "read",
        "mail:user_mailbox.folder:read",
        "mail:user_mailbox.folder:write",
    ),
    "mail.list": _e("GET", _MAILBOX + "/messages", "read", "mail:user_mailbox.message:readonly"),
    "mail.batch_get": _e(
        "POST", _MAILBOX + "/messages/batch_get", "read", "mail:user_mailbox.message:readonly"
    ),
    "mail.get": _e(
        "GET", _MAILBOX + "/messages/{message_id}", "read", "mail:user_mailbox.message:readonly"
    ),
    "mail.search": _e("POST", _MAILBOX + "/search", "read", "mail:user_mailbox.message:readonly"),
    "mail.draft": _e("POST", _MAILBOX + "/drafts", "write", "mail:user_mailbox.message:modify"),
    "mail.send_draft": _e(
        "POST", _MAILBOX + "/drafts/{draft_id}/send", "send", "mail:user_mailbox.message:send"
    ),
    "mail.modify": _e(
        "PUT",
        _MAILBOX + "/messages/{message_id}/modify",
        "write",
        "mail:user_mailbox.message:modify",
    ),
    "mail.trash": _e(
        "POST",
        _MAILBOX + "/messages/{message_id}/trash",
        "write",
        "mail:user_mailbox.message:modify",
    ),
    # 任务
    "task.list": _e("GET", "/task/v2/tasks", "read", "task:task:read", "task:task:write"),
    "task.get": _e(
        "GET", "/task/v2/tasks/{task_guid}", "read", "task:task:read", "task:task:write"
    ),
    "task.create": _e("POST", "/task/v2/tasks", "write", "task:task:write", "task:task:writeonly"),
    "task.update": _e(
        "PATCH", "/task/v2/tasks/{task_guid}", "write", "task:task:write", "task:task:writeonly"
    ),
    "task.delete": _e(
        "DELETE", "/task/v2/tasks/{task_guid}", "write", "task:task:write", "task:task:delete"
    ),
    "task.tasklists": _e(
        "GET", "/task/v2/tasklists", "read", "task:tasklist:read", "task:tasklist:write"
    ),
    "task.tasklist_tasks": _e(
        "GET",
        "/task/v2/tasklists/{tasklist_guid}/tasks",
        "read",
        "task:tasklist:read",
        "task:tasklist:write",
    ),
    "task.comment": _e(
        "POST", "/task/v2/comments", "write", "task:comment:write", "task:comment:writeonly"
    ),
    # 云文档：搜索、云空间、文档、评论
    "docs.search": _e("POST", "/search/v2/doc_wiki/search", "read", "search:docs:read"),
    "drive.files": _e(
        "GET",
        "/drive/v1/files",
        "read",
        "space:document:retrieve",
        "drive:drive:readonly",
        "drive:drive",
    ),
    "docx.raw": _e(
        "GET",
        "/docx/v1/documents/{document_id}/raw_content",
        "read",
        "docx:document:readonly",
        "docx:document",
    ),
    "docx.create": _e(
        "POST", "/docx/v1/documents", "write", "docx:document:create", "docx:document"
    ),
    "docx.append": _e(
        "POST",
        "/docx/v1/documents/{document_id}/blocks/{block_id}/children",
        "write",
        "docx:document:write_only",
        "docx:document",
    ),
    "drive.comments": _e(
        "GET",
        "/drive/v1/files/{file_token}/comments",
        "read",
        "docs:document.comment:read",
        "drive:drive",
        "drive:drive:readonly",
        "docs:doc",
        "docs:doc:readonly",
        "sheets:spreadsheet",
        "sheets:spreadsheet:readonly",
    ),
    "drive.comment": _e(
        "POST",
        "/drive/v1/files/{file_token}/new_comments",
        "write",
        "docs:document.comment:create",
        "docs:document.comment:write_only",
    ),
    # 电子表格
    "sheets.query": _e(
        "GET", "/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query", "read", *_SHEET_READ
    ),
    "sheets.read": _e(
        "GET",
        "/sheets/v2/spreadsheets/{spreadsheet_token}/values_batch_get",
        "read",
        *_SHEET_READ,
    ),
    "sheets.write": _e(
        "PUT", "/sheets/v2/spreadsheets/{spreadsheet_token}/values", "write", *_SHEET_WRITE
    ),
    "sheets.append": _e(
        "POST",
        "/sheets/v2/spreadsheets/{spreadsheet_token}/values_append",
        "write",
        *_SHEET_WRITE,
    ),
    "sheets.create": _e(
        "POST",
        "/sheets/v3/spreadsheets",
        "write",
        "sheets:spreadsheet:create",
        "sheets:spreadsheet",
        "drive:drive",
    ),
    # 多维表格
    "bitable.tables": _e(
        "GET",
        "/bitable/v1/apps/{app_token}/tables",
        "read",
        "bitable:app",
        "bitable:app:readonly",
        "base:table:read",
    ),
    "bitable.fields": _e(
        "GET", _TABLE + "/fields", "read", "bitable:app", "bitable:app:readonly", "base:field:read"
    ),
    "bitable.search": _e(
        "POST",
        _TABLE + "/records/search",
        "read",
        "bitable:app",
        "bitable:app:readonly",
        "base:record:read",
    ),
    "bitable.create": _e("POST", _TABLE + "/records", "write", "bitable:app", "base:record:create"),
    "bitable.update": _e(
        "PUT", _TABLE + "/records/{record_id}", "write", "bitable:app", "base:record:update"
    ),
    "bitable.delete": _e(
        "DELETE", _TABLE + "/records/{record_id}", "write", "bitable:app", "base:record:delete"
    ),
    # 知识库
    "wiki.spaces": _e("GET", "/wiki/v2/spaces", "read", "wiki:space:retrieve", *_WIKI_READ),
    "wiki.node": _e("GET", "/wiki/v2/spaces/get_node", "read", "wiki:node:read", *_WIKI_READ),
    "wiki.nodes": _e(
        "GET", "/wiki/v2/spaces/{space_id}/nodes", "read", "wiki:node:retrieve", *_WIKI_READ
    ),
    "wiki.create": _e(
        "POST", "/wiki/v2/spaces/{space_id}/nodes", "write", "wiki:node:create", "wiki:wiki"
    ),
    # 通讯录
    "contact.search": _e("POST", "/contact/v3/users/search", "read", "contact:user:search"),
    "contact.user": _e(
        "GET",
        "/contact/v3/users/{user_id}",
        "read",
        "contact:user.base:readonly",
        "contact:contact.base:readonly",
        "contact:contact:readonly",
    ),
    # 审批（用户级接口）
    "approval.tasks": _e("GET", "/approval/v4/tasks", "read", "approval:task:read"),
    "approval.initiated": _e(
        "GET", "/approval/v4/instances/initiated", "read", "approval:instance:read"
    ),
    "approval.instance": _e(
        "GET", "/approval/v4/instances/detail", "read", "approval:instance:read"
    ),
    "approval.approve": _e("POST", "/approval/v4/tasks/pass", "write", "approval:task:write"),
    "approval.reject": _e("POST", "/approval/v4/tasks/refuse", "write", "approval:task:write"),
    # OKR
    "okr.cycles": _e("GET", "/okr/v2/cycles", "read", "okr:okr.period:readonly"),
    "okr.objectives": _e(
        "GET", "/okr/v2/cycles/{cycle_id}/objectives", "read", "okr:okr.content:readonly"
    ),
    "okr.key_results": _e(
        "GET", "/okr/v2/objectives/{objective_id}/key_results", "read", "okr:okr.content:readonly"
    ),
    # 考勤
    "attendance.results": _e(
        "POST", "/attendance/v1/user_tasks/query", "read", "attendance:task:readonly"
    ),
}

# Needed by an API above in addition to its gating permission: reading a mail returns the
# subject, addresses and body only with their field permissions.
EXTRA_SCOPES = frozenset(
    {
        "mail:user_mailbox.message.subject:read",
        "mail:user_mailbox.message.address:read",
        "mail:user_mailbox.message.body:read",
    }
)

# Literal paths first, so `/wiki/v2/spaces/get_node` never matches as a `{space_id}`.
_ORDERED = sorted(ENDPOINTS.values(), key=lambda ep: ep.path.count("{"))


def build(endpoint: Endpoint, **values: str) -> str:
    """Fill a registered template; values must be single opaque path segments."""
    if set(_PARAM.findall(endpoint.path)) != set(values):
        raise ValueError("path_parameters_mismatch")
    for value in values.values():
        if not isinstance(value, str) or not _SEGMENT.match(value):
            raise ValueError("invalid_path_parameter")
    return _PARAM.sub(lambda found: values[found.group(1)], endpoint.path)


def match(method: str, path: str) -> Endpoint | None:
    """The registered endpoint `path` was built from, or None for anything else."""
    for endpoint in _ORDERED:
        found = endpoint.pattern.match(path) if endpoint.method == method else None
        if found is not None:
            if all(_SEGMENT.match(value) for value in found.groupdict().values()):
                return endpoint
            return None
    return None


def denied(endpoint: Endpoint, level: str, scopes: set[str] | frozenset[str]) -> str | None:
    """Why this owner's saved tier and permissions do not reach `endpoint`, or None.

    `scopes` are the permissions both selected for this tier and actually granted; legacy
    grants predate selection and are checked against their fixed read-only set instead.
    """
    if endpoint.kind == "send" and level != "all":
        return "sending_not_authorized"
    if level not in LEVELS_BY_KIND[endpoint.kind]:
        return "outside_selected_authorization"
    if level == "messages_readonly" and not MESSAGE_SCOPES.intersection(endpoint.scopes):
        return "outside_selected_authorization"
    if level == "legacy_readonly":
        if LEGACY_SCOPES.intersection(endpoint.scopes):
            return None
        return "outside_selected_authorization"
    if scopes.intersection(endpoint.scopes) and endpoint.also <= scopes:
        return None
    return "sending_not_authorized" if endpoint.kind == "send" else "selected_permission_missing"


def request_scopes(level: str, allowed: set[str] | frozenset[str]) -> list[str]:
    """What the top two tiers ask the owner to grant: only what the tools use.

    `allowed` is the app's enabled user permissions already narrowed to the tier. Apps
    often enable hundreds of permissions and Feishu refuses an authorization request with
    too many (error 20084), so each API contributes one permission the app has, the most
    specific one first, plus the fixed message-read set and the extra field permissions.
    """
    chosen = set(allowed & (MESSAGE_SCOPES | EXTRA_SCOPES | {"offline_access"}))
    for endpoint in ENDPOINTS.values():
        if level not in LEVELS_BY_KIND[endpoint.kind] or not endpoint.also <= allowed:
            continue
        pick = next((scope for scope in endpoint.scopes if scope in allowed), None)
        if pick is not None:
            chosen |= {pick} | endpoint.also
    return sorted(chosen)


def manifest_scopes() -> frozenset[str]:
    """User permissions the app requests at creation: one accepted permission per API."""
    wanted = set(EXTRA_SCOPES)
    for endpoint in ENDPOINTS.values():
        wanted.add(endpoint.scopes[0])
        wanted |= endpoint.also
    return frozenset(wanted)

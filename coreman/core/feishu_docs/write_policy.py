"""群聊可用的个人工具白名单：不在表里的一律不可用。

locate 只返回定位字段；create 直接执行；target 目标可信才直接执行；confirm 一律确认。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

MAX_WRITES_PER_TURN = 30

_LOCATE = (
    "feishu_search_docs feishu_drive_files feishu_file_info feishu_wiki_spaces feishu_wiki_nodes"
    " feishu_wiki_node feishu_calendar_list feishu_calendar_events feishu_calendar_search"
    " feishu_calendar_event feishu_calendar_freebusy feishu_meeting_rooms feishu_task_list"
    " feishu_task_search feishu_task_get feishu_tasklists feishu_tasklist_tasks"
)
_CREATE = (
    "feishu_create_document feishu_create_sheet feishu_create_folder feishu_wiki_create_node"
    " feishu_prepare_upload feishu_save_to_drive feishu_task_create feishu_task_add_subtask"
    " feishu_calendar_create_event"
)
_CONFIRM = (
    "feishu_calendar_update_event feishu_calendar_add_attendees feishu_calendar_delete_event"
    " feishu_calendar_reply feishu_task_update feishu_task_delete feishu_share_document"
    " feishu_move_file feishu_base_delete_record"
)
TARGET_FIELD: dict[str, str] = {
    "feishu_append_document": "document_id",
    "feishu_edit_document": "document_id",
    "feishu_add_document_comment": "file_token",
    "feishu_sheet_write": "spreadsheet_token",
    "feishu_sheet_append": "spreadsheet_token",
    "feishu_sheet_manage": "spreadsheet_token",
    "feishu_base_create_table": "app_token",
    "feishu_base_create_field": "app_token",
    "feishu_base_create_record": "app_token",
    "feishu_base_update_record": "app_token",
    "feishu_base_batch_update_records": "app_token",
    "feishu_rename_file": "token",
    "feishu_copy_file": "token",
    "feishu_task_comment": "task_guid",
}
GROUP_TOOLS: dict[str, str] = {
    **dict.fromkeys(_LOCATE.split(), "locate"),
    **dict.fromkeys(_CREATE.split(), "create"),
    **dict.fromkeys(TARGET_FIELD, "target"),
    **dict.fromkeys(_CONFIRM.split(), "confirm"),
}
_OTHERS = ("attendee_open_ids", "assignee_open_ids", "follower_open_ids")
_KEEP = frozenset(
    {
        "items", "events", "tasks", "files", "nodes", "spaces", "calendars", "rooms",
        "calendar_list", "res_units", "freebusy_list", "docs_entities", "metas", "event", "task",
        "node", "has_more", "page_token", "next_page_token", "total",
        "event_id", "calendar_id", "summary", "start_time", "end_time", "timestamp", "date",
        "timezone", "status", "task_guid", "guid", "tasklist_guid", "completed_at", "due", "name",
        "title", "token", "doc_token", "docs_token", "doc_type", "docs_type", "type", "url",
        "obj_token", "obj_type", "node_token", "space_id", "parent_node_token", "room_id",
        "resource_id", "capacity", "is_busy",
    }
)


def effective_category(name: str, args: Any) -> str:
    category = GROUP_TOOLS[name]
    if category == "create" and any(getattr(args, field, None) for field in _OTHERS):
        return "confirm"
    return category


def trim_locate(data: Any) -> Any:
    """只留白名单字段，未知字段默认丢弃。"""
    if isinstance(data, list):
        return [trim_locate(item) for item in data]
    if isinstance(data, dict):
        return {key: trim_locate(value) for key, value in data.items() if key in _KEEP}
    return data


def args_hash(arguments: dict[str, Any]) -> str:
    raw = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()

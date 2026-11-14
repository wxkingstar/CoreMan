"""写类个人工具的审批单模板：每个工具归到一个渠道模板，写明动作名、哪些参数是对象、哪些是内容。

模板只决定审批单怎么排版；没有登记的写类工具照样被拦截，用兜底模板（全部参数逐项列出）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from coreman.core.feishu_personal import endpoints
from coreman.core.feishu_personal.tools import TOOLS as PERSONAL_TOOLS


@dataclass(frozen=True)
class Template:
    """一个渠道的排版：`channel` 是审批单上的渠道，`target` / `body` 是按顺序展示的参数名。

    不在这两组里的参数一律列在「其他参数」下，不会被省略。
    """

    channel: str
    target: tuple[str, ...] = ()
    body: tuple[str, ...] = ()
    action: str = ""


# 消息：渠道在提交时读会话后定为私聊或群聊；对象是会话名，原话取自会话。
MESSAGE = Template("message", body=("text",))
GROUP_ADMIN = Template("group", target=("name", "chat_id"), body=("description", "member_open_ids"))
MAIL = Template(
    "mail",
    target=("to", "cc", "bcc", "message_id", "reply_to_message_id", "forward_message_id"),
    body=("subject", "body", "mark_read", "move_to_folder"),
)
CALENDAR = Template(
    "calendar",
    target=("calendar_id", "event_id"),
    body=(
        "summary",
        "description",
        "start_time",
        "end_time",
        "location",
        "attendee_open_ids",
        "room_ids",
        "rsvp_status",
    ),
)
TASK = Template(
    "task",
    target=("task_guid", "parent_task_guid", "tasklist_guid"),
    body=(
        "summary",
        "description",
        "content",
        "due",
        "completed",
        "assignee_open_ids",
        "follower_open_ids",
    ),
)
DOC = Template(
    "doc",
    target=(
        "document_id",
        "file_token",
        "token",
        "spreadsheet_token",
        "sheet_id",
        "range",
        "app_token",
        "table_id",
        "record_id",
        "space_id",
        "parent_node_token",
        "folder_token",
    ),
    body=(
        "title",
        "name",
        "new_title",
        "command",
        "pattern",
        "content",
        "text",
        "blocks",
        "values",
        "fields",
        "records",
        "member_open_ids",
        "permission",
    ),
)
APPROVAL = Template(
    "approval",
    target=("instance_code", "task_id", "task_ids", "approval_code"),
    body=("comment", "form", "approvers", "cc", "transfer_open_id"),
)
# 兜底：没有专门排版的工具，全部参数逐项列在「其他参数」下。
FALLBACK = Template("other")

_GROUPS: dict[Template, dict[str, str]] = {
    MESSAGE: {
        "feishu_send_message": "发送消息",
        "feishu_reply_message": "回复消息",
        "feishu_forward_message": "转发消息",
        "feishu_edit_message": "编辑已发消息",
        "feishu_recall_message": "撤回已发消息",
        "feishu_add_reaction": "添加表情回应",
        "feishu_pin_message": "置顶消息",
        "feishu_send_file": "发送文件",
    },
    GROUP_ADMIN: {
        "feishu_create_chat": "建群",
        "feishu_add_chat_members": "拉人进群",
    },
    MAIL: {
        "feishu_mail_send": "发送邮件",
        "feishu_mail_create_draft": "保存邮件草稿",
        "feishu_mail_modify": "标记或移动邮件",
        "feishu_mail_trash": "删除邮件",
    },
    CALENDAR: {
        "feishu_calendar_create_event": "创建日程",
        "feishu_calendar_update_event": "修改日程",
        "feishu_calendar_delete_event": "删除日程",
        "feishu_calendar_add_attendees": "邀请参会人",
        "feishu_calendar_reply": "回复日程邀请",
    },
    TASK: {
        "feishu_task_create": "创建任务",
        "feishu_task_add_subtask": "创建子任务",
        "feishu_task_update": "修改任务",
        "feishu_task_delete": "删除任务",
        "feishu_task_comment": "评论任务",
    },
    DOC: {
        "feishu_create_folder": "新建文件夹",
        "feishu_move_file": "移动文件",
        "feishu_copy_file": "复制文件",
        "feishu_rename_file": "重命名文件",
        "feishu_create_document": "新建文档",
        "feishu_append_document": "追加文档内容",
        "feishu_edit_document": "修改文档内容",
        "feishu_share_document": "修改文档权限",
        "feishu_add_document_comment": "评论文档",
        "feishu_sheet_write": "写入表格",
        "feishu_sheet_append": "追加表格行",
        "feishu_create_sheet": "新建表格",
        "feishu_sheet_manage": "调整工作表",
        "feishu_base_create_table": "新建数据表",
        "feishu_base_create_field": "新建字段",
        "feishu_base_create_record": "新增多维表记录",
        "feishu_base_update_record": "修改多维表记录",
        "feishu_base_batch_update_records": "批量修改多维表记录",
        "feishu_base_delete_record": "删除多维表记录",
        "feishu_wiki_create_node": "新建知识库页面",
        "feishu_save_to_drive": "保存文件到云空间",
    },
    APPROVAL: {
        "feishu_approval_approve": "通过飞书审批",
        "feishu_approval_reject": "拒绝飞书审批",
        "feishu_approval_transfer": "转交飞书审批",
        "feishu_approval_submit": "发起飞书审批",
        "feishu_approval_recall": "撤回飞书审批",
        "feishu_approval_remind": "催办飞书审批",
    },
    FALLBACK: {
        "feishu_okr_add_progress": "添加 OKR 进展",
        "feishu_okr_update_progress": "修改 OKR 进展",
    },
}
ENTRIES: dict[str, Template] = {
    name: replace(template, action=action)
    for template, names in _GROUPS.items()
    for name, action in names.items()
}

# 往会话里发新内容：必须带 chat_id 与锚点，提交前与执行前都重新读会话。
SENDS = frozenset(
    {
        "feishu_send_message",
        "feishu_reply_message",
        "feishu_forward_message",
        "feishu_send_file",
    }
)
# 作用在一条已有消息上：必须带 chat_id，目标消息要在这个会话里且没被撤回。
ON_MESSAGE = frozenset(
    {"feishu_edit_message", "feishu_recall_message", "feishu_add_reaction", "feishu_pin_message"}
)
# 只能动本人发的消息。
OWN_MESSAGE = frozenset({"feishu_edit_message", "feishu_recall_message"})
# 同一内容发给多个会话可以合并成一张单。
MERGEABLE = frozenset({"feishu_send_message"})
# 入参里带中转文件编号的工具：暂存时文件复制到临时桶，批准后取回（见 attachments.py）。
FILE_TOOLS = frozenset(
    {"feishu_send_file", "feishu_save_to_drive", "feishu_mail_send", "feishu_mail_create_draft"}
)
# 所属接口是写类、自己却不调飞书的工具：只签发一个上传到平台中转区的链接。对助理直接执行。
UNGATED = frozenset({"feishu_prepare_upload"})
# 只用于去重的参数：同一件事换一个重试编号仍是同一件事。
RETRY_KEYS = frozenset({"uuid", "idempotency_key", "client_token"})
# 不是个人工具、经平台代理执行的写操作：个人助理无人在场时由代理自动提交审批（见 proxied.py）。
# 审批单的工具名 → 结果回合与页面上说的来源。
SYSTEM_CALL = "systems_call"
CLOUD_CALL = "cloud_call"
PROXIED = {SYSTEM_CALL: "业务系统", CLOUD_CALL: "云服务"}


def is_write(name: str) -> bool:
    """这个个人工具会不会改动飞书里的东西（按它所属接口的 `kind`；`UNGATED` 里的除外）。"""
    item = PERSONAL_TOOLS.get(name)
    if item is None or name in UNGATED:
        return False
    return endpoints.ENDPOINTS[item.endpoint].kind != "read"


def entry(name: str) -> Template:
    """工具的模板；没登记的用兜底，动作名就是工具名。"""
    return ENTRIES.get(name) or replace(FALLBACK, action=name)

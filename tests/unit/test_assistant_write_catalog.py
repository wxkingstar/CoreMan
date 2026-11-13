"""接口读写分类与写类工具模板的台账。

个人助理的闸门按 `endpoints.py` 的 `kind` 区分读写：读类直接执行，其余一律拦截。新增接口时
必须在这里的两张清单里登记一次（这就是一次有意识的分类）；非 GET 的接口登记为读要另外点名。
"""

from __future__ import annotations

import inspect

from coreman.core.assistants.approvals import catalog
from coreman.core.feishu_personal import endpoints
from coreman.core.feishu_personal.tools import TOOLS

# 会改动飞书里东西的接口（kind 为 write 或 send）。
WRITES = {
    "approval.approve", "approval.recall", "approval.reject", "approval.remind",
    "approval.submit", "approval.transfer",
    "bitable.batch_update", "bitable.create", "bitable.create_field", "bitable.create_table",
    "bitable.delete", "bitable.update",
    "calendar.attendees", "calendar.create", "calendar.delete", "calendar.reply",
    "calendar.update",
    "docs.update", "docx.append", "docx.create",
    "drive.comment", "drive.copy", "drive.folder", "drive.move", "drive.rename", "drive.share",
    "drive.upload",
    "im.chat_add_members", "im.chat_create", "im.edit", "im.forward", "im.pin", "im.reaction",
    "im.reaction_delete", "im.recall", "im.reply", "im.send", "im.upload_file",
    "im.upload_image",
    "mail.draft", "mail.modify", "mail.send_draft", "mail.trash",
    "okr.progress_create", "okr.progress_update",
    "sheets.append", "sheets.create", "sheets.structure", "sheets.write",
    "task.comment", "task.create", "task.delete", "task.subtask", "task.update",
    "wiki.create",
}  # fmt: skip
# 用 POST 但只做查询的接口（搜索、批量取、忙闲）：登记为读之前确认过不产生任何改动。
READ_BY_POST = {
    "approval.templates", "attendance.results", "bitable.search", "calendar.freebusy",
    "calendar.primary", "calendar.search", "contact.departments", "contact.search",
    "docs.fetch", "docs.search", "drive.metas", "im.search", "mail.batch_get", "mail.search",
    "minutes.search", "task.search", "vc.rooms", "vc.search",
}  # fmt: skip


def test_every_endpoint_is_listed_as_read_or_write() -> None:
    actual = {key for key, item in endpoints.ENDPOINTS.items() if item.kind != "read"}
    assert actual == WRITES, "新增或改动了接口：先确认它读还是写，再更新这张清单"


def test_reads_that_do_not_use_get_are_named() -> None:
    odd = {
        key
        for key, item in endpoints.ENDPOINTS.items()
        if item.kind == "read" and item.method != "GET"
    }
    assert odd == READ_BY_POST


def test_no_get_endpoint_is_treated_as_a_write_by_mistake() -> None:
    # 反过来不设限（GET 记成写只会多拦一次），这里只确认清单里没有笔误。
    assert WRITES <= set(endpoints.ENDPOINTS)


def test_every_write_tool_has_a_template_and_reads_have_none() -> None:
    writes = {name for name in TOOLS if catalog.is_write(name)}
    assert writes == set(catalog.ENTRIES), "新增了写类工具：在 catalog 里给它选一个模板"
    assert all(item.action for item in catalog.ENTRIES.values())
    assert not catalog.is_write("feishu_search_messages")
    assert not catalog.is_write("feishu_authorize") and not catalog.is_write("no_such_tool")


def test_the_only_ungated_write_endpoint_tool_never_calls_feishu() -> None:
    """`feishu_prepare_upload` 挂在写类接口名下（决定谁能看到它），自己只签发一个平台的上传链接。

    它的函数体里没有任何一次飞书调用（`call(...)`、`call.upload`、`call.download`）；
    哪天有了，这条用例失败，就该把它从 `UNGATED` 里拿掉。
    """
    assert catalog.UNGATED == {"feishu_prepare_upload"}
    for name in catalog.UNGATED:
        assert endpoints.ENDPOINTS[TOOLS[name].endpoint].kind != "read"
        source = inspect.getsource(TOOLS[name].run)
        assert "call(" not in source and "call.upload" not in source
        assert "call.download" not in source and "api_request" not in source
        assert name not in catalog.ENTRIES and name not in catalog.FILE_TOOLS


def test_unlisted_tools_fall_back_to_the_generic_template() -> None:
    found = catalog.entry("feishu_future_write")
    assert (found.channel, found.target, found.body) == ("other", (), ())
    assert found.action == "feishu_future_write"


def test_template_fields_exist_on_the_tools_that_use_them() -> None:
    for name, template in catalog.ENTRIES.items():
        fields = set(TOOLS[name].args.model_fields)
        # 模板按渠道共用：每个工具至少要有一个参数落在模板里，兜底模板除外。
        shown = set(template.target) | set(template.body) | {"message_id", "upload_id"}
        if template.channel != "other":
            assert fields & shown, name


def test_message_tool_sets_are_consistent() -> None:
    message_tools = {n for n, t in catalog.ENTRIES.items() if t.channel == "message"}
    assert catalog.SENDS | catalog.ON_MESSAGE == message_tools
    assert catalog.OWN_MESSAGE <= catalog.ON_MESSAGE and catalog.MERGEABLE <= catalog.SENDS


def test_new_endpoints_request_no_new_permission() -> None:
    """普通机器人的应用清单与授权请求不变：新接口复用已有接口的权限。"""
    table = endpoints.ENDPOINTS
    assert table["im.reaction_delete"].scopes == table["im.reaction"].scopes
    assert table["im.chat"].scopes == table["im.chats"].scopes
    assert table["im.chat"].kind == "read" and table["im.reaction_delete"].kind == "write"

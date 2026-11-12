"""写操作的暂存与提交：不执行、参数加密入库、提交审批；去重、上限、Tower 不可用。"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import approvals, keys
from coreman.core.assistants.approvals import attachments, service, tower
from coreman.core.db.models import AssistantApproval
from coreman.core.feishu_personal.policy import Scope
from coreman.core.feishu_personal.toolsets import files as file_tools
from coreman.core.timeutils import utcnow
from tests.integration.approval_helpers import (
    CFG,
    OWNER,
    TEXT,
    FakeFeishu,
    FakeRelay,
    assistant_turn,
    install,
    install_files,
    message,
    reply,
)
from tests.integration.assistant_helpers import SECRET, raw, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
STORED = "SELECT args_enc::text FROM assistant_approvals WHERE id = :id"


async def submit(
    session: AsyncSession,
    scope: Scope,
    tool: str,
    arguments: dict[str, Any],
    files: FakeRelay | None = None,
) -> Any:
    out = await approvals.submit(session, None, scope, tool, arguments, cfg=CFG, files=files)  # type: ignore[arg-type]
    await session.commit()
    return out


async def count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(AssistantApproval)) or 0)


async def test_write_is_staged_sealed_and_submitted_not_executed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    feishu, fake = install(monkeypatch)
    bot, owner, scope = await assistant_turn(db_session)
    out = await submit(
        db_session,
        scope,
        "feishu_reply_message",
        reply(background="他上周问过", research="查了报价表"),
    )
    assert out["status"] == "pending_approval" and feishu.writes == []
    row = await db_session.get(AssistantApproval, out["approval_id"])
    assert row is not None and row.status == "pending" and row.tool == "feishu_reply_message"
    # 元数据：渠道、会话、锚点（没给时就是被回复的那条）、话题、来源那一轮。
    assert (row.channel, row.chat_id, row.anchor_message_id, row.topic) == (
        "p2p", "oc_li", "om_2", "报价"
    )  # fmt: skip
    assert row.origin_event_id == scope.task.inbound_event_id
    assert row.session_key == "oc_assistant"
    assert timedelta(hours=71) < row.expires_at - utcnow() <= timedelta(hours=72)
    # 内容：库里是密文，取出来是原样的参数（不含 approval）。
    stored = str(await raw(db_session, STORED, id=row.id))
    assert SECRET not in stored and "李四" not in stored
    assert row.args_enc is not None
    call = row.args_enc["calls"][0]
    assert call["arguments"] == {k: v for k, v in reply().items() if k != "approval"}
    assert row.args_enc["abstract"] == "私聊 → 李四"
    # 审批单：发起人是所有者，摘要只有渠道和对象，正文由平台生成并附上助理的说明。
    sent = fake.submitted[0]
    assert sent["requester"] == owner.login_name and sent["caller"] == bot.bot_key
    assert sent["external_id"] == row.external_id == "cm-pa-" + row.id.hex
    assert sent["abstract"] == "私聊 → 李四" and SECRET not in sent["abstract"]
    assert "│ 李四：周五能给报价吗？" in sent["content"] and f"│ {TEXT}" in sent["content"]
    assert "背景：\n│ 他上周问过" in sent["content"]
    # 请求体是暂存参数的规范化形式，哈希按它算。
    assert sent["body"] == tower.request_body("feishu_reply_message", row.args_enc["calls"])
    assert row.request_hash == tower.request_hash(CFG, row.external_id, sent["body"], bot.bot_key)
    assert row.approval_id == "apv-" + row.external_id[-6:]


async def test_same_content_returns_the_existing_approval(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fake = install(monkeypatch)
    _, _, scope = await assistant_turn(db_session)
    first = await submit(db_session, scope, "feishu_reply_message", reply())
    # 只换了重试编号：仍是同一件事。
    again = await submit(db_session, scope, "feishu_reply_message", {**reply(), "uuid": "u2"})
    assert again == {
        "status": "pending_approval",
        "approval_id": first["approval_id"],
        "duplicate": True,
    }
    assert len(fake.submitted) == 1 and await count(db_session) == 1
    # 已经执行过的同一份内容：告诉助理已执行，不再提交。
    row = await db_session.get(AssistantApproval, first["approval_id"])
    assert row is not None
    row.status, row.executed_at = "executed", utcnow()
    await db_session.commit()
    done = await submit(db_session, scope, "feishu_reply_message", reply())
    assert done["status"] == "already_executed" and len(fake.submitted) == 1
    # 内容不同就是另一张单。
    other = await submit(db_session, scope, "feishu_reply_message", {**reply(), "text": "周五"})
    assert other["status"] == "pending_approval" and "duplicate" not in other


async def test_other_refusals_say_retrying_cannot_help(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    feishu, _ = install(monkeypatch)
    _, _, scope = await assistant_turn(db_session)

    async def refuse(cfg: object, **kw: object) -> object:
        raise tower.TowerError("body_too_large", 400)

    monkeypatch.setattr(tower, "submit", refuse)
    out = await submit(db_session, scope, "feishu_reply_message", reply())
    assert out["error"] == "approval_rejected" and out["detail"] == "body_too_large"
    assert "不会" in out["hint"] and "稍后" not in out["hint"]
    assert await count(db_session) == 0 and feishu.writes == []


async def test_tower_unavailable_means_nothing_is_staged_or_executed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    feishu, fake = install(monkeypatch)
    _, _, scope = await assistant_turn(db_session)
    fake.down = True
    out = await submit(db_session, scope, "feishu_reply_message", reply())
    assert out["error"] == "approval_service_unavailable" and out["detail"] == "tower_unavailable"
    assert await count(db_session) == 0 and feishu.writes == []
    # 部署没配审批系统：同样不可用。
    unconfigured = await approvals.submit(
        db_session,
        None,
        scope,
        "feishu_reply_message",
        reply(),
        cfg=None,  # type: ignore[arg-type]
    )
    assert unconfigured["error"] == "approval_service_unavailable"
    assert await count(db_session) == 0


async def test_unknown_requester_is_reported_as_a_missing_account(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    _, owner, scope = await assistant_turn(db_session)

    async def unknown(cfg: tower.Config, **kw: Any) -> tower.Submitted:
        raise tower.TowerError("unknown_approvers", 400)

    monkeypatch.setattr(tower, "submit", unknown)
    out = await submit(db_session, scope, "feishu_reply_message", reply())
    assert out["error"] == "tower_account_missing"
    owner.login_name = None
    await db_session.commit()
    assert (await submit(db_session, scope, "feishu_reply_message", reply()))["error"] == (
        "no_login_name"
    )


async def test_at_most_ten_pending(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    _, _, scope = await assistant_turn(db_session)
    for index in range(service.MAX_PENDING):
        made = await submit(
            db_session,
            scope,
            "feishu_task_create",
            {"summary": f"第 {index} 件", "client_token": "t"},
        )
        assert made["status"] == "pending_approval"
    out = await submit(
        db_session, scope, "feishu_task_create", {"summary": "第 11 件", "client_token": "t"}
    )
    assert out["error"] == "too_many_pending" and out["limit"] == 10
    assert await count(db_session) == 10


@pytest.mark.parametrize(
    ("history", "code"),
    [
        ([message("om_3", OWNER, "我来回"), message("om_2")], "owner_replied_after_anchor"),
        ([message("om_3", text="不用了"), message("om_2")], "newer_messages_after_anchor"),
        ([message("om_9")], "message_gone"),
    ],
)
async def test_chat_moved_on_is_rejected_before_anything_is_staged(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    history: list[dict[str, Any]],
    code: str,
) -> None:
    _, fake = install(monkeypatch, FakeFeishu(history))
    _, _, scope = await assistant_turn(db_session)
    out = await submit(db_session, scope, "feishu_reply_message", reply())
    assert out["error"] == code and out["hint"]
    assert fake.submitted == [] and await count(db_session) == 0


async def test_same_text_to_several_chats_is_one_approval(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fake = install(monkeypatch)
    _, _, scope = await assistant_turn(db_session)
    arguments = {
        "receive_id": "oc_li",
        "receive_id_type": "chat_id",
        "text": "明早九点开会",
        "uuid": "a",
        "approval": {
            "chat_id": "oc_li",
            "anchor_message_id": "om_2",
            "more_recipients": [
                {"receive_id": "oc_wang", "anchor_message_id": "om_2", "uuid": "b"}
            ],
        },
    }
    out = await submit(db_session, scope, "feishu_send_message", arguments)
    row = await db_session.get(AssistantApproval, out["approval_id"])
    assert row is not None and row.args_enc is not None
    assert [c["chat_id"] for c in row.args_enc["calls"]] == ["oc_li", "oc_wang"]
    assert row.args_enc["calls"][1]["arguments"]["uuid"] == "b"
    assert len(fake.submitted) == 1 and "对象（2 个会话）" in fake.submitted[0]["content"]
    # 最多合并 10 个会话；别的工具不能合并；同一个会话不能出现两次。
    many = [{"receive_id": f"oc_{i}", "uuid": f"u{i}"} for i in range(10)]
    too_many = {**arguments, "approval": {"chat_id": "oc_li", "more_recipients": many}}
    assert (await submit(db_session, scope, "feishu_send_message", too_many))["error"] == (
        "invalid_tool_or_arguments"
    )
    twice = {"chat_id": "oc_li", "more_recipients": [{"receive_id": "oc_li", "uuid": "c"}]}
    assert (
        await submit(db_session, scope, "feishu_send_message", {**arguments, "approval": twice})
    )["error"] == "duplicate_recipient"


async def test_what_cannot_be_submitted(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fake = install(monkeypatch)
    bot, owner, scope = await assistant_turn(db_session)
    cases: list[tuple[str, dict[str, Any], str]] = [
        # 带文件的操作要有文件中转与临时桶；这里两样都没有。
        ("feishu_save_to_drive", {"upload_id": "x" * 20}, "attachments_not_supported"),
        # 参数不合工具的定义。
        ("feishu_reply_message", {"message_id": "om_2"}, "invalid_tool_or_arguments"),
        ("feishu_reply_message", {**reply(), "approval": {"nope": 1}}, "invalid_tool_or_arguments"),
        # 读类工具不走这里。
        ("feishu_search_messages", {"query": "报价"}, "invalid_tool_or_arguments"),
        # 消息类必须带 chat_id。
        ("feishu_reply_message", {**reply(), "approval": {}}, "chat_id_required"),
        # 合并只给发送消息用。
        (
            "feishu_reply_message",
            reply(more_recipients=[{"receive_id": "oc_wang", "uuid": "b"}]),
            "merge_not_supported",
        ),
    ]
    for tool, arguments, code in cases:
        out = await submit(db_session, scope, tool, arguments)
        assert out.get("error") == code, (tool, out)
    assert fake.submitted == [] and await count(db_session) == 0
    # 数据密钥销毁之后：提交不了，也不抛错。
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    locked = await submit(db_session, scope, "feishu_reply_message", reply())
    assert locked == {"error": "content_locked"} and fake.submitted == []


async def test_content_over_the_limit_is_refused(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fake = install(monkeypatch)
    _, _, scope = await assistant_turn(db_session)
    huge = {"summary": "对账", "description": "长" * 11000, "client_token": "t"}
    out = await submit(db_session, scope, "feishu_task_create", huge)
    assert out["error"] == "content_too_long" and out["limit"] == 10000
    assert fake.submitted == [] and await count(db_session) == 0


async def test_standard_bots_never_reach_the_gate(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """普通机器人的写类工具不暂存：闸门只认助理记录的所有者。"""
    _, fake = install(monkeypatch)
    _, owner, scope = await assistant_turn(db_session)
    standard = await seed_standard(db_session)
    foreign = Scope(
        scope.task, standard, owner.id, "t", "cli_example", OWNER, "zhangsan", "oc_x", scope.event
    )
    out = await submit(db_session, foreign, "feishu_reply_message", reply())
    assert out == {"error": "invalid_tool_or_arguments"}
    assert fake.submitted == [] and await count(db_session) == 0


async def test_file_write_copies_the_upload_into_the_temporary_bucket(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    feishu, fake = install(monkeypatch)
    relay, bucket = install_files(monkeypatch)
    _, _, scope = await assistant_turn(db_session)
    content = f"%PDF {SECRET}".encode()
    arguments = {
        "upload_id": relay.add("报价单.pdf", content),
        "folder_token": "fld_1",
        "approval": {"topic": "报价"},
    }
    out = await submit(db_session, scope, "feishu_save_to_drive", arguments, relay)
    assert out["status"] == "pending_approval" and feishu.writes == [] and feishu.uploads == []
    row = await db_session.get(AssistantApproval, out["approval_id"])
    assert row is not None and row.has_files and row.args_enc is not None
    ref = row.args_enc["calls"][0]["arguments"]["upload_id"]
    digest = hashlib.sha256(content).hexdigest()
    assert {k: v for k, v in ref.items() if k != "key"} == {
        "filename": "报价单.pdf",
        "mime": "application/pdf",
        "size": len(content),
        "sha256": digest,
    }
    # 对象名：前缀加 256 位随机数，不带文件名或任何人的标识；它只在密文里。
    key = ref["key"]
    assert key.startswith("pa/") and len(key) == 3 + 43
    assert bucket.objects == {key: content}
    stored = str(await raw(db_session, STORED, id=row.id))
    assert key not in stored and "报价单" not in stored and digest not in stored
    sent = fake.submitted[0]
    assert f'- "报价单.pdf"（application/pdf，{len(content)} B）' in sent["content"]
    assert f"https://tmp.example.com/{key}?expires=259200" in sent["content"]
    # 被批准的是这份内容：请求体里是文件名、大小与哈希，没有对象名。
    assert json.loads(sent["body"]) == {
        "tool": "feishu_save_to_drive",
        "calls": [
            {
                "folder_token": "fld_1",
                "upload_id": {"filename": "报价单.pdf", "size": len(content), "sha256": digest},
            }
        ],
    }
    assert sent["expires_at"] == row.expires_at
    # 同一份文件重新上传一次再提交：还是同一件事，不再复制。
    again = {**arguments, "upload_id": relay.add("报价单.pdf", content)}
    dup = await submit(db_session, scope, "feishu_save_to_drive", again, relay)
    assert dup == {
        "status": "pending_approval",
        "approval_id": out["approval_id"],
        "duplicate": True,
    }
    assert len(bucket.objects) == 1 and len(fake.submitted) == 1


async def test_mail_attachments_and_chat_files_are_staged_too(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fake = install(monkeypatch)
    relay, bucket = install_files(monkeypatch)
    _, _, scope = await assistant_turn(db_session)
    mail = {
        "to": ["lisi@example.com"],
        "subject": "报价",
        "body": "见附件",
        "attachment_ids": [relay.add("a.pdf", b"aaa"), relay.add("b.pdf", b"bbbb")],
        "uuid": "m1",
    }
    out = await submit(db_session, scope, "feishu_mail_send", mail, relay)
    assert out["status"] == "pending_approval" and len(bucket.objects) == 2
    assert "附件（2 个；" in fake.submitted[0]["content"]
    # 发到会话里的文件按消息类检查：要带 chat_id 与锚点。
    chat = {
        "receive_id": "oc_li",
        "receive_id_type": "chat_id",
        "upload_id": relay.add("c.pdf", b"ccccc"),
        "uuid": "f1",
    }
    refused = await submit(db_session, scope, "feishu_send_file", chat, relay)
    assert refused["error"] == "chat_id_required" and len(bucket.objects) == 2
    sent = await submit(
        db_session,
        scope,
        "feishu_send_file",
        {**chat, "approval": {"chat_id": "oc_li", "anchor_message_id": "om_2"}},
        relay,
    )
    assert sent["status"] == "pending_approval" and len(bucket.objects) == 3
    assert "渠道：私聊\n对象：李四" in fake.submitted[1]["content"]


async def test_file_writes_fail_closed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fake = install(monkeypatch)
    relay, bucket = install_files(monkeypatch)
    _, _, scope = await assistant_turn(db_session)
    good = {"upload_id": relay.add("a.pdf", b"aaa"), "folder_token": "fld_1"}

    async def code(arguments: dict[str, Any], files: FakeRelay | None = relay) -> str:
        out = await submit(db_session, scope, "feishu_save_to_drive", arguments, files)
        return str(out.get("error"))

    # 中转编号无效或已过期；文件超过原工具的上限。
    assert await code({**good, "upload_id": "y" * 20}) == "upload_not_found"
    big = relay.add("big.bin", b"x")
    relay.blobs[big].size = file_tools.DRIVE_LIMIT + 1
    assert await code({**good, "upload_id": big}) == "file_too_large"
    # 临时桶写不进去：不提交。
    bucket.down = True
    assert await code(good) == "attachment_store_unavailable"
    bucket.down = False
    # 审批系统没收下：刚复制的对象删掉。
    fake.down = True
    assert await code(good) == "approval_service_unavailable"
    fake.down = False
    assert bucket.objects == {} and fake.submitted == [] and await count(db_session) == 0
    # 没配临时桶的部署：带文件的写操作不可用，别的照常。
    monkeypatch.setattr(attachments, "store", lambda settings: None)
    assert await code(good) == "attachments_not_supported"
    assert (await submit(db_session, scope, "feishu_reply_message", reply(), relay))["status"] == (
        "pending_approval"
    )

"""审批单的摘要与正文：内容来自实际参数；只截断原话；助理写的说明单独标明。"""

from __future__ import annotations

import pytest

from coreman.core.assistants.approvals import catalog, content
from coreman.core.assistants.approvals.content import Facts

REPLY = {"message_id": "om_1", "text": "可以，周四下班前发你。", "format": "text", "uuid": "u1"}


def _one(arguments: dict[str, object]) -> list[dict[str, object]]:
    return [{"arguments": arguments, "chat_id": "oc_1", "anchor_message_id": "om_1"}]


def test_abstract_carries_only_channel_and_target() -> None:
    reply = catalog.entry("feishu_reply_message")
    facts = [Facts("p2p", "李四", "李四：周五能给报价吗？")]
    assert content.abstract(reply, facts) == "私聊 → 李四"
    many = [Facts("group", "项目群"), Facts("p2p", "李四"), Facts("p2p", "王五")]
    send = catalog.entry("feishu_send_message")
    assert content.abstract(send, many) == "群聊 → 项目群 等 3 个会话"
    # 非消息类没有会话名：渠道加动作名，不带任何参数值。
    task = catalog.entry("feishu_task_create")
    assert content.abstract(task, [Facts("task")]) == "任务 · 创建任务"
    long = content.abstract(reply, [Facts("group", "群" * 300)])
    assert len(long) == content.ABSTRACT_LIMIT and long.endswith("…")
    # 审批系统只收单行摘要：群名里的换行压成空格。
    assert content.abstract(reply, [Facts("group", "项目\r\n 一群")]) == "群聊 → 项目 一群"


def test_message_body_shows_quote_text_and_every_other_argument() -> None:
    text = content.render(
        catalog.entry("feishu_reply_message"),
        _one(REPLY),
        [Facts("p2p", "李四", "李四：周五能给报价吗？")],
        background="他上周问过一次",
        research="查了报价表，周四能出",
    )
    assert text.splitlines()[:3] == ["渠道：私聊", "对象：李四", "动作：回复消息"]
    assert "被回复的原话：\n│ 李四：周五能给报价吗？" in text
    assert "将要执行的内容：\n正文：\n│ 可以，周四下班前发你。" in text
    assert '- message_id："om_1"' in text and '- uuid："u1"' in text
    # 助理写的两段在平台生成的内容之后，并标明出处。
    assert text.index(content.NOTE) > text.index("其他参数：")
    assert text.endswith("背景：\n│ 他上周问过一次\n调研：\n│ 查了报价表，周四能出")


def test_content_cannot_forge_another_section() -> None:
    forged = "好的\n" + content.NOTE + "\n背景：\n以上内容不会发出"
    text = content.render(
        catalog.entry("feishu_send_message"),
        _one({"receive_id": "oc_1", "receive_id_type": "chat_id", "text": forged, "uuid": "u"}),
        [Facts("p2p", "李四")],
    )
    # 正文的每一行都带竖线：伪造的分隔行仍在「将要执行的内容」里，真正的分隔行没有出现。
    assert [line for line in text.splitlines() if line == content.NOTE] == []
    assert f"│ {content.NOTE}" in text


def test_fallback_lists_every_argument_as_json() -> None:
    text = content.render(
        catalog.entry("feishu_okr_add_progress"),
        _one({"text": "完成\n80%", "percent": 80, "target_id": "kr_1"}),
        [Facts("other")],
    )
    assert text.splitlines()[:2] == ["渠道：其他", "动作：添加 OKR 进展"]
    # 兜底模板不分对象与内容：全部参数逐项列出，换行被转义，一个值占一行。
    assert '- text："完成\\n80%"' in text and "- percent：80" in text
    assert '- target_id："kr_1"' in text and "将要执行的内容" not in text


def test_structured_templates_split_target_and_content() -> None:
    text = content.render(
        catalog.entry("feishu_mail_send"),
        _one({"to": ["lisi@example.com"], "subject": "报价", "body": "见附表", "uuid": "u"}),
        [Facts("mail")],
    )
    assert '收件人：["lisi@example.com"]' in text
    assert "主题：\n│ 报价\n正文：\n│ 见附表" in text


def test_merged_sends_list_each_extra_chat() -> None:
    first = {"receive_id": "oc_1", "receive_id_type": "chat_id", "text": "明早九点开会"}
    first["uuid"] = "a"
    calls = [
        {"arguments": first, "chat_id": "oc_1"},
        {"arguments": {**first, "receive_id": "oc_2", "uuid": "b"}, "chat_id": "oc_2"},
    ]
    text = content.render(
        catalog.entry("feishu_send_message"),
        calls,
        [Facts("p2p", "李四", "李四：好"), Facts("p2p", "王五", "王五：收到")],
    )
    assert "对象（2 个会话）：李四、王五" in text
    assert "各会话里最近的一条消息：\n│ 李四：好\n│ 王五：收到" in text
    assert text.count("│ 明早九点开会") == 1
    assert '其余会话的参数：\n- {"receive_id": "oc_2", "uuid": "b"}' in text


def test_only_the_quote_is_truncated() -> None:
    body = "正" * 6000
    text = content.render(
        catalog.entry("feishu_reply_message"),
        _one({**REPLY, "text": body}),
        [Facts("p2p", "李四", "李四：" + "原" * 9000)],
    )
    assert len(text) <= content.LIMIT
    assert body in text and content.CUT in text


def test_too_long_without_the_quote_is_refused() -> None:
    with pytest.raises(content.TooLong):
        content.render(
            catalog.entry("feishu_reply_message"),
            _one({**REPLY, "text": "正" * 10000}),
            [Facts("p2p", "李四", "李四：短")],
        )


def test_quote_is_dropped_when_there_is_no_room_for_it() -> None:
    reply = catalog.entry("feishu_reply_message")
    empty = len(content.render(reply, _one({**REPLY, "text": ""}), [Facts("p2p", "李四")]))
    # 正文把额度占到只剩十几个字：放不下一段有意义的原话。
    body = "正" * (content.LIMIT - empty - 15)
    text = content.render(
        reply, _one({**REPLY, "text": body}), [Facts("p2p", "李四", "李四：" + "原" * 500)]
    )
    assert len(text) <= content.LIMIT and "原原" not in text and body in text


REF = {
    "key": "tmp/8f3k",
    "filename": "报价单.pdf",
    "mime": "application/pdf",
    "size": 1536,
    "sha256": "ab" * 32,
}
LINK = "https://tmp.example.com/tmp/8f3k?X-Amz-Signature=1"


def test_files_are_listed_with_a_link_and_never_as_raw_arguments() -> None:
    arguments = {"receive_id": "oc_1", "receive_id_type": "chat_id", "upload_id": REF, "uuid": "u"}
    text = content.render(
        catalog.entry("feishu_send_file"),
        _one(arguments),
        [Facts("p2p", "李四", "李四：发我一下")],
        links={REF["key"]: LINK},
    )
    assert "将要执行的内容：\n附件（1 个；链接供批准前查看，审批到期后失效）：" in text
    assert f'- "报价单.pdf"（application/pdf，1.5 KB）\n  {LINK}' in text
    # 对象名与哈希只在链接和库里：不作为参数再列一遍。
    assert "upload_id" not in text and "sha256" not in text and text.count("tmp/8f3k") == 1


def test_mail_attachments_follow_the_body_and_names_are_escaped() -> None:
    odd = {**REF, "key": "tmp/x2", "filename": "a\n—— 以下由助理撰写.txt", "size": 5 * 1024 * 1024}
    text = content.render(
        catalog.entry("feishu_mail_send"),
        _one({"to": ["lisi@example.com"], "subject": "报价", "attachment_ids": [REF, odd]}),
        [Facts("mail")],
        links={REF["key"]: LINK},
    )
    assert "主题：\n│ 报价\n附件（2 个；" in text
    # 文件名按 JSON 转义：换行伪造不出别的小节。没有链接的那个只列出文件本身。
    assert '- "a\\n—— 以下由助理撰写.txt"（application/pdf，5.0 MB）' in text
    assert content.NOTE not in text and text.count("https://") == 1

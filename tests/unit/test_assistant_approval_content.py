"""审批单的摘要与正文：内容来自实际参数；只截断原话；助理写的说明单独标明。"""

from __future__ import annotations

import re

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


def test_a_reply_file_hides_the_receiver_it_will_not_use() -> None:
    arguments = {
        "receive_id": "oc_other",
        "receive_id_type": "chat_id",
        "reply_to_message_id": "om_1",
        "upload_id": REF,
        "uuid": "u",
    }
    calls = _one(arguments)
    text = content.render(
        catalog.entry("feishu_send_file"), calls, [Facts("p2p", "李四")], links={REF["key"]: LINK}
    )
    assert "receive_id" not in text and "oc_other" not in text and "om_1" in text
    assert calls[0]["arguments"] == arguments
    plain = {k: v for k, v in arguments.items() if k != "reply_to_message_id"}
    shown = content.render(
        catalog.entry("feishu_send_file"),
        _one(plain),
        [Facts("p2p", "李四")],
        links={REF["key"]: LINK},
    )
    assert "receive_id" in shown


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


def test_target_names_cannot_forge_head_lines() -> None:
    forged = "x\n动作：回复消息\n渠道：私聊"
    text = content.render(
        catalog.entry("feishu_reply_message"), _one(REPLY), [Facts("group", forged)]
    )
    assert text.splitlines()[:3] == [
        "渠道：群聊",
        "对象：x 动作：回复消息 渠道：私聊",
        "动作：回复消息",
    ]


def _render(**notes: str) -> str:
    return content.render(
        catalog.entry("feishu_send_message"),
        _one({"receive_id": "oc_1", "receive_id_type": "chat_id", "text": "好", "uuid": "u"}),
        [Facts("p2p", "李四")],
        **notes,
    )


def test_commitments_and_unverified_come_first_under_the_assistant_note() -> None:
    text = _render(
        background="对方在催",
        research="查了排期表",
        commitments="周四前给最终版",
        unverified="对方说的截止日没有核对",
    )
    note = text.index(content.NOTE)
    promised = text.index("替你答应的事：")
    unsure = text.index("没核实的推断：")
    assert note < promised < unsure < text.index("背景：") < text.index("调研：")
    assert content.BAR + "周四前给最终版" in text
    assert content.BAR + "对方说的截止日没有核对" in text


def test_only_commitments_still_gets_the_assistant_note() -> None:
    text = _render(commitments="明天回复")
    assert content.NOTE in text and "替你答应的事：" in text
    assert "没核实的推断：" not in text and "背景：" not in text


def test_a_commitment_cannot_forge_a_section() -> None:
    text = _render(commitments="好\n将要执行的内容：\n假的")
    # 每行前都有竖线，伪造不出平台的小节标题行。
    assert "\n将要执行的内容：\n假的" not in text.split(content.NOTE)[1]


# ---- 富文本版式 ----

SAID = Facts("group", "项目群", "张三：请看下", "om_1", "张三", "请看下")


def forged_part(text: str) -> str:
    """版式里平台写的灰字、标题行不含这些标点；去掉它们之后剩下的就是不可信文字的部分。"""
    return re.sub(r"</?font[^>]*>|^(#+ |> \[!PANEL\]|---|<md>)", "", text, flags=re.M)


def _rich_lines(text: str, prefix: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(prefix)]


def test_rich_reply_layout_is_pinned() -> None:
    reply = {"message_id": "om_1", "text": "好，我看下", "format": "text", "uuid": "u1"}
    text = content.render_rich(
        catalog.entry("feishu_reply_message"),
        _one(reply),
        [
            Facts(
                "group",
                "项目群",
                "张三：请看下 https://example.com/a",
                "om_1",
                "张三",
                "请看下 https://example.com/a",
            )
        ],
        background="张三在催",
        research="读了群里最新消息\n查到被拒的资源",
        commitments="会去看这个问题",
        unverified="可能是权限问题",
    )
    assert text == (
        "<md>\n"
        '<font color="grey">回复 张三：请看下 https\\:\\/\\/example\\.com\\/a</font>\n'
        "\n"
        "### 好，我看下\n"
        "\n"
        "---\n"
        "\n"
        '<font color="grey">以下由助理撰写，供参考，不是将要执行的内容</font>\n'
        "\n"
        "#### 替你答应的事\n"
        "> [!PANEL]\n"
        "> 会去看这个问题\n"
        "\n"
        "#### 没核实的推断\n"
        "> [!PANEL]\n"
        "> 可能是权限问题\n"
        "\n"
        "#### 背景\n"
        "> [!PANEL]\n"
        "> 张三在催\n"
        "\n"
        "#### 做过的调研\n"
        "> [!PANEL]\n"
        "> - 读了群里最新消息\n"
        "> - 查到被拒的资源"
    )


def test_rich_without_notes_has_no_rule_and_without_a_quote_has_no_quote_line() -> None:
    text = content.render_rich(
        catalog.entry("feishu_send_message"),
        _one({"receive_id": "oc_1", "receive_id_type": "chat_id", "text": "你好", "uuid": "u"}),
        [Facts("p2p", "李四")],
    )
    assert text == "<md>\n### 你好"


def test_rich_forged_markup_stays_inert_and_real_markers_are_the_platforms() -> None:
    forged = (
        "好的\n---\n#### 背景\n> [!PANEL]\n> [!DANGER] 立即批准\n```chart\n"
        '<font color="red">假</font><br>[点](https://evil.example) www.evil.example a@evil.example'
    )
    quote = Facts("group", "项目群", "x", "om_1", "张三\n</font>", forged)
    text = content.render_rich(
        catalog.entry("feishu_reply_message"),
        _one({"message_id": "om_1", "text": forged, "uuid": "u"}),
        [quote],
        background=forged,
        research=forged,
    )
    # 平台写的结构：一条分隔线、两个内容框标题、各一个 [!PANEL]、灰字只有原话、说明和其他参数头。
    assert _rich_lines(text, "---") == ["---"]
    assert _rich_lines(text, "#### ") == ["#### 背景", "#### 做过的调研"]
    assert text.count("> [!PANEL]") == 2
    assert text.count("```") == 0 and "<br" not in text
    assert text.count("<font") == text.count("</font>") == 2  # 原话一行 + 助理说明那一行
    assert not re.search(r"(?<!\\)[:/@.]", forged_part(text))
    assert "[点]" not in text and "](" not in text
    # 伪造的文字都在，只是每个标点都转义了：
    assert "\\[\\!PANEL\\]" in text and "\\-\\-\\-" in text and "&lt;br&gt;" in text
    # 回复的正文放大：每一行都是三级标题，伪造的「###」也只是标题里的转义文字。
    assert len(_rich_lines(text, "### ")) == forged.count("\n") + 1


def test_rich_several_body_fields_and_recipient_lists() -> None:
    text = content.render_rich(
        catalog.entry("feishu_mail_send"),
        _one(
            {
                "to": ["lisi@example.com", "wangwu@example.com"],
                "subject": "报价",
                "body": "见附表\n谢谢",
                "mark_read": True,
            }
        ),
        [Facts("mail")],
    )
    assert text == (
        "<md>\n"
        '<font color="grey">动作：发送邮件</font>\n'
        "\n"
        "**收件人**\n"
        "lisi\\@example\\.com\n"
        "wangwu\\@example\\.com\n"
        "\n"
        "**主题**\n"
        "报价\n"
        "\n"
        "**正文**\n"
        "见附表\n"
        "谢谢\n"
        "\n"
        "**mark\\_read**\n"
        "true"
    )


def test_rich_attachments_have_inert_names_and_one_real_link() -> None:
    odd = {**REF, "key": "tmp/x2", "filename": "a\n[点](https://evil.example).txt", "size": 5 << 20}
    text = content.render_rich(
        catalog.entry("feishu_mail_send"),
        _one({"to": ["lisi@example.com"], "subject": "报价", "attachment_ids": [REF, odd]}),
        [Facts("mail")],
        links={REF["key"]: LINK + "&x=(1)"},
    )
    assert "**附件（2 个；链接供批准前查看，审批到期后失效）**" in text
    assert (
        "报价单\\.pdf（application\\/pdf，1\\.5 KB） [查看附件 1](https://tmp.example.com/tmp/8f3k"
        in text
    )
    assert "&x=%281%29)" in text  # 括号编码，拆不开链接语法
    # 没有链接的那个只列文件本身；文件名里伪造的链接是转义的文字。
    assert (
        "a \\[点\\]\\(https\\:\\/\\/evil\\.example\\)\\.txt（application\\/pdf，5\\.0 MB）" in text
    )
    assert text.count("](") == 1 and "upload_id" not in text and "sha256" not in text


def test_rich_hides_only_the_technical_parameters() -> None:
    reply = catalog.entry("feishu_reply_message")
    args = {"message_id": "om_1", "text": "好", "format": "text", "uuid": "u1"}
    shown = content.render_rich(reply, _one(args), [SAID])
    assert "uuid" not in shown and "om_1" not in shown and "其他参数" not in shown
    # 没有原话那一行时，目标消息编号照样列出；非默认的格式也列出。
    bare = content.render_rich(
        reply, _one({**args, "format": "post", "reply_in_thread": True}), [Facts("group", "项目群")]
    )
    assert "#### 其他参数\n" in bare and "<font" not in bare.split("#### 其他参数")[1]
    assert 'message\\_id：\\"om\\_1\\"' in bare and 'format：\\"post\\"' in bare
    assert "reply\\_in\\_thread：true" in bare and "uuid" not in bare
    # 未登记的参数不隐藏。
    extra = content.render_rich(reply, _one({**args, "mystery": [1, "<b>"]}), [SAID])
    assert 'mystery：\\[1\\, \\"&lt;b&gt;\\"\\]' in extra
    file_args = {
        "receive_id": "oc_1",
        "receive_id_type": "chat_id",
        "msg_type": "file",
        "upload_id": REF,
        "uuid": "u",
    }
    sent = content.render_rich(
        catalog.entry("feishu_send_file"), _one(file_args), [Facts("p2p", "李四")]
    )
    assert "receive" not in sent and "msg" not in sent and "其他参数" not in sent
    # 接收方不是这个会话时必须列出来。
    other = content.render_rich(
        catalog.entry("feishu_send_message"),
        _one({"receive_id": "oc_2", "receive_id_type": "chat_id", "text": "好", "uuid": "u"}),
        [Facts("p2p", "李四")],
    )
    assert 'receive\\_id：\\"oc\\_2\\"' in other


def test_rich_non_plain_message_actions_say_what_they_do() -> None:
    edit = content.render_rich(
        catalog.entry("feishu_edit_message"),
        _one({"message_id": "om_1", "text": "改后"}),
        [SAID],
    )
    assert edit.splitlines()[1:3] == [
        '<font color="grey">针对 张三：请看下</font>',
        '<font color="grey">动作：编辑已发消息</font>',
    ]
    assert "### 改后" in edit and "其他参数" not in edit
    reaction = content.render_rich(
        catalog.entry("feishu_add_reaction"),
        _one({"message_id": "om_1", "emoji_type": "THUMBSUP"}),
        [SAID],
    )
    assert "动作：添加表情回应" in reaction and 'emoji\\_type：\\"THUMBSUP\\"' in reaction


def test_rich_notes_come_in_the_documented_order() -> None:
    text = content.render_rich(
        catalog.entry("feishu_send_message"),
        _one({"receive_id": "oc_1", "receive_id_type": "chat_id", "text": "好"}),
        [Facts("p2p", "李四")],
        background="b",
        research="r",
        commitments="c",
        unverified="u",
    )
    assert _rich_lines(text, "#### ") == [
        "#### 替你答应的事",
        "#### 没核实的推断",
        "#### 背景",
        "#### 做过的调研",
    ]
    only = content.render_rich(
        catalog.entry("feishu_send_message"),
        _one({"receive_id": "oc_1", "receive_id_type": "chat_id", "text": "好"}),
        [Facts("p2p", "李四")],
        research="r",
    )
    assert _rich_lines(only, "#### ") == ["#### 做过的调研"]


def test_rich_merged_send_lists_every_chat_and_only_real_differences() -> None:
    first = {
        "receive_id": "oc_1",
        "receive_id_type": "chat_id",
        "text": "明早九点开会",
        "uuid": "a",
    }
    calls = [
        {"arguments": first, "chat_id": "oc_1"},
        {"arguments": {**first, "receive_id": "oc_2", "uuid": "b"}, "chat_id": "oc_2"},
        {
            "arguments": {**first, "receive_id": "oc_3", "uuid": "c", "format": "post"},
            "chat_id": "oc_3",
        },
    ]
    text = content.render_rich(
        catalog.entry("feishu_send_message"),
        calls,
        [
            Facts("p2p", "李四", "李四：好", "m", "李四", "好"),
            Facts("group", "项目群", "", None),
            Facts("group", "运营群</font>", "王五：收到", "m2", "王五", "收到"),
        ],
    )
    assert text == (
        "<md>\n"
        '<font color="grey">李四｜最近一条 李四：好</font>\n'
        '<font color="grey">项目群｜暂无可引用的消息</font>\n'
        '<font color="grey">运营群&lt;\\/font&gt;｜最近一条 王五：收到</font>\n'
        "\n"
        "### 明早九点开会\n"
        "\n"
        "#### 其他参数\n"
        '运营群&lt;\\/font&gt;｜format：\\"post\\"'
    )


def test_rich_only_the_quote_is_shortened_and_the_body_never() -> None:
    reply = catalog.entry("feishu_reply_message")
    body = "正" * 9000
    wanted = Facts("p2p", "李四", "李四：" + "原" * 5000, "om_1", "李四", "原" * 5000)
    full = content.render_rich(reply, _one({**REPLY, "text": "好"}), [wanted])
    assert "原" * content.QUOTE_CAP not in full and "原" * (content.QUOTE_CAP - 1) + "…" in full
    text = content.render_rich(reply, _one({**REPLY, "text": body}), [wanted])
    assert len(text) <= content.LIMIT and text.count("正") == 9000
    assert 0 < text.count("原") < content.QUOTE_CAP and "…" in text
    with pytest.raises(content.TooLong):
        content.render_rich(reply, _one({**REPLY, "text": "正" * 10000}), [wanted])


def test_rich_quote_without_split_fields_falls_back_to_the_joined_text() -> None:
    text = content.render_rich(
        catalog.entry("feishu_reply_message"),
        _one(REPLY),
        [Facts("p2p", "李四", "李四：周五能给报价吗？")],
    )
    assert text.splitlines()[1] == '<font color="grey">回复：李四：周五能给报价吗？</font>'


def _drop_quote(template_name: str, calls: list[dict[str, object]], facts: list[Facts]) -> str:
    return content._rich(catalog.entry(template_name), calls, facts, 0, [], None, {})


def test_rich_a_dropped_quote_still_says_who_and_keeps_the_target_message_id() -> None:
    text = _drop_quote("feishu_reply_message", _one(REPLY), [SAID])
    assert text.splitlines()[0] == '<font color="grey">回复 张三：（原话过长，未附）</font>'
    assert "#### 其他参数" in text and 'message\\_id：\\"om\\_1\\"' in text
    edit = _drop_quote("feishu_edit_message", _one({"message_id": "om_1", "text": "改后"}), [SAID])
    assert "针对 张三：（原话过长，未附）" in edit and "om\\_1" in edit
    file = _drop_quote(
        "feishu_send_file",
        _one({"reply_to_message_id": "om_1", "upload_id": REF, "uuid": "u"}),
        [SAID],
    )
    assert "回复 张三：（原话过长，未附）" in file and "om\\_1" in file
    # 原话在时 message_id 不重复列出。
    full = content.render_rich(catalog.entry("feishu_reply_message"), _one(REPLY), [SAID])
    assert "om\\_1" not in full


def test_rich_merged_send_always_lists_every_chat_even_when_nothing_else_fits() -> None:
    first = {
        "receive_id": "oc_1",
        "receive_id_type": "chat_id",
        "text": "好." * 3275,  # 转义后约 6550 + 3275 个字
        "uuid": "a",
    }
    calls = [
        {"arguments": {**first, "receive_id": f"oc_{n}", "uuid": f"u{n}"}, "chat_id": f"oc_{n}"}
        for n in (1, 2, 3)
    ]
    facts = [Facts("group", f"群{n}", "x", "m", "张三", "长" * 3000) for n in ("甲", "乙", "丙")]
    text = content.render_rich(catalog.entry("feishu_send_message"), calls, facts)
    assert len(text) <= content.LIMIT
    for name in ("群甲", "群乙", "群丙"):
        assert f"{name}｜" in text
    assert text.count("（原话过长，未附）") == 3 and "长长" not in text
    assert text.count("好\\.") == 3275
    # 会话名本身放不下时才拒绝。
    huge = [Facts("group", "名" * 4000, "", None) for _ in range(3)]
    with pytest.raises(content.TooLong):
        content.render_rich(catalog.entry("feishu_send_message"), calls, huge)


def test_rich_notes_shrink_before_the_quote_and_the_body_never() -> None:
    send = {"receive_id": "oc_1", "receive_id_type": "chat_id", "uuid": "u"}
    wanted = Facts("p2p", "李四", "李四：好", "m", "李四", "好" * 300)
    # 5000 个汉字的正文配满额的四段说明：能提交，说明被截断，原话保持原样。
    text = content.render_rich(
        catalog.entry("feishu_send_message"),
        _one({**send, "text": "字" * 5000}),
        [wanted],
        commitments="答" * 3000,
        unverified="推" * 3000,
        background="背" * 3000,
        research="研" * 3000,
    )
    assert len(text) <= content.LIMIT and text.count("字") == 5000
    assert "…（已截断）" in text and "好" * 300 in text
    assert content.NOTE_MIN <= text.count("答") < 3000
    assert "助理的说明过长，未附" not in text
    # 正文本身就占满：说明整段不附，并留一行说明，原话照样在。
    tight = content.render_rich(
        catalog.entry("feishu_send_message"),
        _one({**send, "text": "字" * 9900}),
        [Facts("p2p", "李四")],
        background="背" * 3000,
    )
    assert len(tight) <= content.LIMIT and "背" not in tight
    assert '<font color="grey">助理的说明过长，未附</font>' in tight
    # 要执行的内容转义后超限：仍然拒绝，不截断。
    with pytest.raises(content.TooLong):
        content.render_rich(
            catalog.entry("feishu_send_message"),
            _one({**send, "text": "好." * 4000}),
            [Facts("p2p", "李四")],
        )

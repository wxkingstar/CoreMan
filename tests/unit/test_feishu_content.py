"""飞书消息内容 → 可读文字：各消息类型、卡片 1.0 / 2.0 / 默认结构、模板卡片与上限。"""

import json

import pytest

from coreman.core.platforms.feishu_content import (
    MAX_TEXT,
    UPGRADE_PLACEHOLDER,
    Image,
    card_text,
    render,
    sent_text,
)


def _text(msg_type: str, content: object, **kwargs: object) -> str:
    rendered = render(msg_type, content, **kwargs)  # type: ignore[arg-type]
    assert rendered is not None
    return rendered.text()


def test_text_mentions_take_names_longest_key_first() -> None:
    names = {"@_user_1": "张三", "@_user_10": "李四"}
    assert (
        _text("text", {"text": "@_user_10 和 @_user_1 看下"}, names=names) == "@李四 和 @张三 看下"
    )


def test_post_keeps_order_links_ats_and_images() -> None:
    post = {
        "zh_cn": {
            "title": "周报",
            "content": [
                [
                    {"tag": "text", "text": "见"},
                    {"tag": "a", "text": "文档", "href": "https://example.test/d"},
                    {"tag": "at", "user_id": "@_user_1", "user_name": ""},
                ],
                [{"tag": "img", "image_key": "img_1"}],
                [{"tag": "code_block", "language": "PYTHON", "text": "print(1)"}],
            ],
        }
    }
    rendered = render("post", json.dumps(post), names={"@_user_1": "王五"})
    assert rendered is not None
    assert rendered.segments == [
        "周报",
        "见[文档](https://example.test/d)@王五",
        Image("img_1"),
        "print(1)",
    ]
    assert rendered.images == ["img_1"]
    assert rendered.text() == "周报\n见[文档](https://example.test/d)@王五\n[图片]\nprint(1)"


def test_card_v1_div_fields_note_and_select() -> None:
    card = {
        "header": {"title": {"tag": "plain_text", "content": "审批"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "**金额** 100"}},
            {
                "tag": "div",
                "fields": [{"is_short": True, "text": {"tag": "lark_md", "content": "部门：销售"}}],
            },
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "备注"}]},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "select_static",
                        "placeholder": {"tag": "plain_text", "content": "结论"},
                        "options": [
                            {"text": {"tag": "plain_text", "content": "同意"}, "value": "y"}
                        ],
                    }
                ],
            },
        ],
    }
    assert card_text(card) == "审批\n**金额** 100\n部门：销售\n备注\n[结论: 同意]"


def test_card_i18n_template_and_degraded_shapes() -> None:
    i18n = {
        "i18n_header": {"en_us": {"title": {"content": "Hi"}}},
        "i18n_elements": {"en_us": [{"tag": "markdown", "content": "body"}]},
    }
    assert card_text(i18n) == "Hi\nbody"
    template = {"type": "template", "data": {"template_id": "t", "template_variable": {"a": "x"}}}
    assert card_text(template) == "x"
    degraded = {"title": "标题", "elements": [[{"tag": "text", "text": "正文"}]]}
    assert card_text(degraded) == "标题\n正文"
    assert card_text({"elements": [[{"tag": "text", "text": UPGRADE_PLACEHOLDER}]]}) == ""


def test_card_depth_and_size_are_bounded() -> None:
    nested: dict = {"tag": "markdown", "content": "底"}
    for _ in range(50):
        nested = {"tag": "column_set", "columns": [{"tag": "column", "elements": [nested]}]}
    assert card_text({"body": {"elements": [nested]}}) == ""
    big = {"body": {"elements": [{"tag": "markdown", "content": "甲" * (MAX_TEXT * 2)}]}}
    assert len(card_text(big)) == MAX_TEXT


@pytest.mark.parametrize(
    ("msg_type", "content", "expected"),
    [
        ("file", {"file_key": "f", "file_name": "a.pdf"}, "[文件: a.pdf]"),
        ("folder", {"file_key": "f", "file_name": "资料"}, "[文件夹: 资料]"),
        ("audio", {"file_key": "f", "duration": 2000}, "[语音消息，飞书不提供文字内容]"),
        ("media", {"file_key": "f", "file_name": "v.mp4"}, "[视频: v.mp4]"),
        ("sticker", {"file_key": "f"}, "[表情包]"),
        ("hongbao", {"text": "[红包]"}, "[红包]"),
        ("share_chat", {"chat_id": "oc_x"}, "[群名片]"),
        ("share_user", {"user_id": "ou_x"}, "[个人名片]"),
        ("location", {"name": "上海"}, "[位置] 上海"),
        ("video_chat", {"topic": "周会", "start_time": "1"}, "[视频会议] 周会"),
        ("vote", {"topic": "午饭", "options": ["面", "饭"]}, "[投票] 午饭：面 / 饭"),
        (
            "calendar",
            {"summary": "评审", "start_time": "1790000000000", "end_time": "1790003600000"},
            "[日程] 评审（2026-09-21 14:13 UTC – 2026-09-21 15:13 UTC）",
        ),
        (
            "system",
            {
                "template": "{from_user} invited {to_chatters}",
                "from_user": ["甲"],
                "to_chatters": ["乙", "丙"],
            },
            "[系统消息] 甲 invited 乙、丙",
        ),
        (
            "todo",
            {
                "summary": {"title": "", "content": [[{"tag": "text", "text": "交周报"}]]},
                "due_time": "1790000000000",
            },
            "[任务] 交周报（截止 2026-09-21 14:13 UTC）",
        ),
    ],
)
def test_other_message_types_render_to_a_line(msg_type: str, content: dict, expected: str) -> None:
    assert _text(msg_type, content) == expected


def test_unknown_malformed_and_empty_content_render_nothing() -> None:
    assert render("mystery", {"x": 1}) is None
    assert render("text", "{not json") is None
    assert render("text", "[]") is None
    assert render("text", {"text": "  "}) is None
    assert render("text", "x" * 300_000) is None


def test_sent_text_skips_card_entity_references() -> None:
    assert sent_text("interactive", {"type": "card", "data": {"card_id": "c1"}}) is None
    card = {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "好"}]}}
    assert sent_text("interactive", card) == "好"
    assert (
        sent_text("post", {"zh_cn": {"content": [[{"tag": "md", "text": "**粗**"}]]}}) == "**粗**"
    )

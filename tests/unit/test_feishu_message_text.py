"""飞书消息内容抽成可读文字：各类消息、卡片（1.0、2.0、降级骨架）、坏数据与超长内容。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from coreman.core.feishu_personal import message_text as mt
from coreman.core.platforms.feishu_content import UPGRADE_PLACEHOLDER


def item(kind: str, content: Any) -> dict[str, Any]:
    raw = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return {"msg_type": kind, "body": {"content": raw}}


CARD_2 = {
    "schema": "2.0",
    "config": {"update_multi": True},
    "header": {
        "title": {"tag": "plain_text", "content": "周报提醒"},
        "subtitle": {"tag": "plain_text", "content": ""},
    },
    "body": {
        "elements": [
            {"tag": "markdown", "content": "**张三**本周\n完成了三件事"},
            {
                "tag": "img",
                "img_key": "img_v3_a",
                "alt": {"tag": "plain_text", "content": "示意图"},
            },
            {
                "tag": "column_set",
                "columns": [
                    {"tag": "column", "elements": [{"tag": "markdown", "content": "李四跟进"}]},
                    {"tag": "column", "elements": [{"tag": "markdown", "content": "李四跟进"}]},
                ],
            },
            {"tag": "button", "text": {"tag": "plain_text", "content": "查看详情"}},
        ]
    },
}
CARD_1 = {
    "header": {"title": {"tag": "plain_text", "content": "审批通知"}},
    "elements": [
        {"tag": "div", "text": {"tag": "lark_md", "content": "张三提交了请假申请"}},
        {
            "tag": "div",
            "fields": [{"is_short": True, "text": {"tag": "lark_md", "content": "3 天"}}],
        },
        {
            "tag": "action",
            "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": "同意"}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "拒绝"}},
            ],
        },
    ],
}
SKELETON = {
    "title": None,
    "elements": [
        [
            {"tag": "img", "image_key": "img_v3_b"},
            {"tag": "text", "text": "请升级客户端查看"},
            {"tag": "text", "text": ""},
        ]
    ],
}


def test_card_2_reads_header_nested_elements_and_counts_images() -> None:
    out = mt.item_text(item("interactive", CARD_2))
    assert out.startswith("[卡片·含 1 张图] 周报提醒")
    for part in ("**张三**本周 完成了三件事", "李四跟进", "查看详情"):
        assert part in out
    assert "\n" not in out
    assert mt.card_image_keys(item("interactive", CARD_2)) == ["img_v3_a"]


def test_card_1_reads_div_fields_and_buttons() -> None:
    out = mt.item_text(item("interactive", CARD_1))
    assert out.startswith("[卡片] 审批通知")
    for part in ("张三提交了请假申请", "3 天", "同意", "拒绝"):
        assert part in out
    assert mt.card_image_keys(item("interactive", CARD_1)) == []


def test_degraded_skeleton_keeps_its_text_and_image() -> None:
    out = mt.item_text(item("interactive", SKELETON))
    assert out == "[卡片·含 1 张图] [图片]请升级客户端查看"
    assert mt.card_image_keys(item("interactive", SKELETON)) == ["img_v3_b"]


def test_skeleton_with_only_the_upgrade_placeholder_is_unreadable() -> None:
    skeleton = {"title": None, "elements": [[{"tag": "text", "text": UPGRADE_PLACEHOLDER}]]}
    assert mt.item_text(item("interactive", skeleton)) == mt.CARD_EMPTY


def test_template_card_reads_every_variable() -> None:
    card = {
        "type": "template",
        "data": {
            "template_id": "ctp_1",
            "template_variable": {"name": "张三", "amount": "1200 元", "reason": "差旅报销"},
        },
    }
    out = mt.item_text(item("interactive", card))
    assert out.startswith("[卡片] ")
    for part in ("张三", "1200 元", "差旅报销"):
        assert part in out


def test_i18n_card_reads_one_language_only() -> None:
    card = {
        "i18n_header": {
            "zh_cn": {"title": {"tag": "plain_text", "content": "中文标题"}},
            "en_us": {"title": {"tag": "plain_text", "content": "English title"}},
        },
        "i18n_elements": {
            "zh_cn": [{"tag": "markdown", "content": "中文正文"}],
            "en_us": [{"tag": "markdown", "content": "English body"}],
        },
    }
    out = mt.item_text(item("interactive", card))
    assert "中文标题" in out and "中文正文" in out
    assert "English" not in out


def test_card_table_cells_are_readable() -> None:
    card = {
        "schema": "2.0",
        "body": {
            "elements": [
                {
                    "tag": "table",
                    "columns": [
                        {"name": "n", "display_name": "姓名", "data_type": "text"},
                        {"name": "d", "display_name": "天数", "data_type": "text"},
                    ],
                    "rows": [{"n": "王五", "d": "三天"}],
                }
            ]
        },
    }
    out = mt.item_text(item("interactive", card))
    assert "王五" in out and "三天" in out


def test_card_without_text_says_so() -> None:
    assert mt.item_text(item("interactive", {"elements": []})) == mt.CARD_EMPTY
    only_image = {"elements": [[{"tag": "img", "image_key": "k"}]]}
    assert mt.item_text(item("interactive", only_image)) == "[卡片·含 1 张图] [图片]"
    combo = {"elements": [{"tag": "img_combination", "img_list": [{"img_key": "a"}]}]}
    assert mt.item_text(item("interactive", combo)) == "[卡片·含 1 张图] [图片]"
    assert mt.card_image_keys(item("interactive", combo)) == ["a"]


def test_post_counts_images_and_videos_in_a_leading_marker() -> None:
    post = {
        "title": "周报",
        "content": [
            [{"tag": "text", "text": "本周"}, {"tag": "img", "image_key": "k"}],
            [{"tag": "media", "file_key": "f"}, {"tag": "a", "text": "链接"}],
        ],
    }
    assert mt.item_text(item("post", post)) == "[含 1 张图、1 个视频] 周报 本周 链接"
    only_media = {"content": [[{"tag": "media", "file_key": "f"}, {"tag": "text", "text": "看"}]]}
    assert mt.item_text(item("post", only_media)) == "[含 1 个视频] 看"
    assert mt.item_text(item("post", {"content": [[{"tag": "text", "text": "好"}]]})) == "好"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[含 2 张图] 好的", "好的"),
        ("[含 1 张图、3 个视频] 好的", "好的"),
        ("[含 1 个视频]", ""),
        ("[含 1 张图]", ""),
        ("[卡片·含 1 张图] 通知", "[卡片·含 1 张图] 通知"),
        ("普通文字 [含 1 张图]", "普通文字 [含 1 张图]"),
    ],
)
def test_without_marker_strips_only_a_leading_post_marker(text: str, expected: str) -> None:
    assert mt.without_marker(text) == expected


@pytest.mark.parametrize(
    ("kind", "content", "expected"),
    [
        ("text", {"text": "你好"}, "你好"),
        ("image", {"image_key": "k"}, "[图片]"),
        ("file", {"file_key": "k", "file_name": "报价.pdf"}, "[文件：报价.pdf]"),
        ("file", {"file_key": "k"}, "[文件]"),
        ("audio", {"file_key": "k"}, "[语音]"),
        ("media", {"file_key": "k", "file_name": "demo.mp4"}, "[视频：demo.mp4]"),
        ("sticker", {"file_key": "k"}, "[表情包]"),
        ("share_chat", {"chat_id": "oc_1"}, "[分享的群名片]"),
        ("share_user", {"user_id": "ou_1"}, "[分享的个人名片]"),
        ("merge_forward", {}, "[合并转发的聊天记录]"),
        ("system", {"template": "x"}, "[system消息]"),
        ("todo", {}, "[todo消息]"),
        ("", {}, "[未知类型消息]"),
    ],
)
def test_other_message_types(kind: str, content: Any, expected: str) -> None:
    assert mt.item_text(item(kind, content)) == expected


@pytest.mark.parametrize("kind", ["text", "post", "interactive", "file", "media", "image"])
@pytest.mark.parametrize("raw", ["{坏的", "null", "[1, 2]", '"字符串"', ""])
def test_malformed_content_never_raises(kind: str, raw: str) -> None:
    assert isinstance(mt.item_text(item(kind, raw)), str)
    assert mt.card_image_keys(item(kind, raw)) == []
    assert isinstance(mt.item_text({"msg_type": kind, "body": None}), str)
    assert isinstance(mt.item_text({}), str)


def test_oversize_content_is_truncated() -> None:
    big = "字" * 10_000
    for one in (
        item("text", {"text": big}),
        item("post", {"content": [[{"tag": "text", "text": big}, {"tag": "img"}]]}),
        item("interactive", {"elements": [{"tag": "markdown", "content": big}]}),
    ):
        out = mt.item_text(one)
        assert len(out) <= mt.LIMIT and "…" in out
    # 图片标记在开头，截断不会切掉它。
    card = item("interactive", {"elements": [{"tag": "markdown", "content": big}, {"tag": "img"}]})
    assert mt.item_text(card).startswith("[卡片·含 1 张图] ")


def test_deeply_nested_card_is_bounded() -> None:
    node: dict[str, Any] = {"tag": "markdown", "content": "深处"}
    for _ in range(500):
        node = {"tag": "column", "elements": [node]}
    assert mt.item_text(item("interactive", {"elements": [node]})) == mt.CARD_EMPTY


@pytest.mark.parametrize("kind", ["interactive", "post", "text"])
def test_hostile_nesting_and_oversize_never_raise(kind: str) -> None:
    nested = "[" * 12_000 + "]" * 12_000
    for raw in (nested, '{"a":' * 12_000 + "1" + "}" * 12_000):
        out = mt.item_text(item(kind, raw))
        assert isinstance(out, str) and out
        assert mt.card_image_keys(item(kind, raw)) == []
    huge = '{"text": "' + "字" * 300_000 + '"}'
    out = mt.item_text(item(kind, huge))
    assert isinstance(out, str) and len(out) <= mt.LIMIT
    assert "字字字" not in out

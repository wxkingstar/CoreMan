import copy
import json
import uuid
from datetime import UTC, datetime

from coreman.runtime.gateway_feishu.inbound import normalize_event

BOT = uuid.uuid4()
KW = dict(
    bot_id=BOT,
    app_id="cli_a",
    bot_open_id="ou_bot",
    gateway_instance="gateway",
    now=datetime.now(UTC),
)
RAW = {
    "header": {"app_id": "cli_a", "event_type": "im.message.receive_v1", "event_id": "ev1"},
    "event": {
        "sender": {
            "sender_type": "user",
            "sender_id": {"user_id": "employee", "open_id": "ou_employee"},
        },
        "message": {
            "message_id": "om1",
            "chat_id": "oc1",
            "chat_type": "group",
            "message_type": "text",
            "content": json.dumps({"text": "@_user_1 hello"}),
            "mentions": [{"key": "@_user_1", "id": {"open_id": "ou_bot"}}],
        },
    },
}


def test_admission_requires_exact_bot_mention_and_human():
    message = normalize_event(RAW, **KW)
    assert message is not None
    assert message.sender.platform_user_id == "employee"
    assert message.parts[0].text == "hello"
    assert message.reply_context["message_id"] == "om1"
    other = copy.deepcopy(RAW)
    other["event"]["message"]["mentions"][0]["id"]["open_id"] = "other_bot"
    assert normalize_event(other, **KW) is None
    other = copy.deepcopy(RAW)
    other["event"]["sender"]["sender_type"] = "app"
    assert normalize_event(other, **KW) is None
    assert normalize_event(RAW, **{**KW, "app_id": "other"}) is None
    assert normalize_event(RAW, **{**KW, "bot_open_id": ""}) is None


def test_no_guessing_identity_or_remote_media_url():
    raw = copy.deepcopy(RAW)
    raw["event"]["sender"]["sender_id"] = {"open_id": "app_specific_openid"}
    raw["event"]["message"].update(
        message_type="image",
        content=json.dumps({"image_key": "img_x", "url": "http://169.254.169.254"}),
    )
    message = normalize_event(raw, **KW)
    assert message is not None and message.sender.platform_user_id == ""
    assert message.parts[0].ref == {"message_id": "om1", "file_key": "img_x", "type": "image"}
    assert normalize_event({"header": []}, **KW) is None
    assert normalize_event({"header": {"app_id": "cli_a"}, "event": "malformed"}, **KW) is None


def test_post_preserves_top_level_files_from_real_feishu_shape():
    raw = copy.deepcopy(RAW)
    raw["event"]["message"].update(
        message_type="post",
        content=json.dumps(
            {
                "title": "库存检查",
                "content_v2": [[{"tag": "text", "text": "请检查附件"}]],
                "files": [
                    {
                        "file_key": "file_v3_test",
                        "file_name": "inventory-test.csv",
                        "is_folder": False,
                    }
                ],
            }
        ),
    )

    message = normalize_event(raw, **KW)

    assert message is not None
    assert [part.model_dump() for part in message.parts] == [
        {"type": "text", "text": "库存检查"},
        {"type": "text", "text": "请检查附件"},
        {
            "type": "file",
            "ref": {"message_id": "om1", "file_key": "file_v3_test", "type": "file"},
            "filename": "inventory-test.csv",
        },
    ]


def test_post_ignores_folder_and_unsafe_file_entries():
    raw = copy.deepcopy(RAW)
    raw["event"]["message"].update(
        message_type="post",
        content=json.dumps(
            {
                "content_v2": [],
                "files": [
                    {"file_key": "folder_key", "file_name": "folder", "is_folder": True},
                    {"file_key": "../secret", "file_name": "secret.txt", "is_folder": False},
                    {"file_key": "safe_key", "file_name": "safe.txt", "is_folder": False},
                ],
            }
        ),
    )

    message = normalize_event(raw, **KW)

    assert message is not None
    assert [part.model_dump() for part in message.parts] == [
        {
            "type": "file",
            "ref": {"message_id": "om1", "file_key": "safe_key", "type": "file"},
            "filename": "safe.txt",
        }
    ]


def test_sender_preserves_union_id_without_inventing_user_id():
    raw = copy.deepcopy(RAW)
    raw["event"]["sender"]["sender_id"] = {"open_id": "ou_app", "union_id": "on_stable"}
    message = normalize_event(raw, **KW)
    assert message is not None
    assert message.sender.model_dump().get("union_id") == "on_stable"
    assert message.sender.platform_user_id == ""


def _click_event(value, *, user_id="employee", event_id="ev_click"):
    return {
        "header": {
            "app_id": "cli_a",
            "event_type": "card.action.trigger",
            "event_id": event_id,
            "tenant_key": "tenant",
        },
        "event": {
            "operator": {"user_id": user_id, "open_id": "ou_employee", "union_id": "on_employee"},
            "context": {"open_chat_id": "oc_group", "open_message_id": "om_card"},
            "action": {"tag": "button", "value": value},
        },
    }


def _rendered_value(requester="employee"):
    from coreman.core.feishu_cards.compile import compile_reply

    text = '```card:actions\n{"buttons": [{"text": "继续分析退款原因", "reply": true}]}\n```'
    card = compile_reply(text, requester=requester).cards[0]
    [row] = card["body"]["elements"]
    return row["columns"][0]["elements"][0]["behaviors"][0]["value"]


def test_reply_button_click_becomes_the_askers_next_message():
    value = _rendered_value()
    message = normalize_event(_click_event(value), **KW)
    assert message is not None
    # 会话类型先按群记，网关应答前按真实私聊记录改判；群里视同 @ 了机器人。
    assert message.kind == "message" and message.chat_type == "group"
    assert message.mentions_bot
    # 发出去的就是按钮上的字。
    assert [p.model_dump() for p in message.parts] == [{"type": "text", "text": "继续分析退款原因"}]
    assert message.chat_id == "oc_group"
    assert message.sender.platform_user_id == "employee"
    # 与打字的消息一样带上 union_id，身份按同一套规则核验。
    assert message.sender.union_id == "on_employee"
    # 回答引用被点的那张卡片；置灰要用的信息跟着入站走，等这一轮真正开始再用。
    assert message.reply_context["message_id"] == "om_card"
    assert message.reply_context["reply_button"] == {
        "message_id": "om_card",
        "row": value["row"],
        "buttons": value["buttons"],
        "label": "继续分析退款原因",
        "requester": "employee",
    }
    # 同一行只算一次：平台重推、连点都落到同一个去重键上。
    again = normalize_event(_click_event(value, event_id="ev_again"), **KW)
    assert again is not None
    assert again.message_id == message.message_id == f"action:om_card:{value['row']}"
    # 找不到按钮行：退回按事件去重。
    message = normalize_event(_click_event({**value, "row": None}), **KW)
    assert message is not None and message.message_id == "action:ev_click"


def test_value_cannot_choose_the_chat_type():
    """value 里写的会话类型一律不认：飞书回调不带它，只能由服务端判定。"""
    message = normalize_event(_click_event({**_rendered_value(), "chat": "single"}), **KW)
    assert message is not None and message.chat_type == "group"
    assert message.reply_context["chat_type"] == "group"


def test_malformed_or_anonymous_reply_clicks_are_dropped():
    value = _rendered_value()
    assert normalize_event(_click_event(value, user_id=""), **KW) is None
    assert normalize_event(_click_event({**value, "text": "x" * 41}), **KW) is None
    assert normalize_event(_click_event({**value, "text": ""}), **KW) is None
    assert normalize_event(_click_event({**value, "requester": None}), **KW) is None
    raw = _click_event(value)
    raw["event"]["context"] = {"open_chat_id": "oc_group"}
    assert normalize_event(raw, **KW) is None
    assert normalize_event(_click_event(value), **{**KW, "app_id": "other"}) is None


def _form_submit(task_id, form, *, event_id="ev_form"):
    raw = _click_event({"task_id": task_id}, event_id=event_id)
    raw["event"]["action"]["form_value"] = form
    return raw


def test_credential_form_is_sealed_before_persistence():
    from coreman.core.crypto import Cipher
    from coreman.core.personal_credentials.policy import sealed_aad

    cipher = Cipher(b"\x07" * 32)
    rid = uuid.uuid4()
    raw = _form_submit(f"credential@{rid}", {"DEMO_PIN": "pin-778899"})
    message = normalize_event(raw, cipher=cipher, **KW)
    assert message is not None and message.kind == "card_action"
    assert message.card_action["card_type"] == "credential"
    assert "selected" not in message.card_action
    assert message.raw["event"]["action"]["form_value"] == {}
    assert "pin-778899" not in json.dumps(message.model_dump(mode="json"), ensure_ascii=False)
    opened = cipher.decrypt(message.card_action["sealed"], sealed_aad(rid))
    assert json.loads(opened) == {"DEMO_PIN": "pin-778899"}
    # 只改落库副本，不动 SDK 交来的原始对象。
    assert raw["event"]["action"]["form_value"] == {"DEMO_PIN": "pin-778899"}


def test_credential_form_without_cipher_is_dropped():
    raw = _form_submit(f"credential@{uuid.uuid4()}", {"DEMO_PIN": "pin-778899"})
    assert normalize_event(raw, **KW) is None


def test_malformed_credential_task_id_is_ignored():
    from coreman.core.crypto import Cipher

    raw = _form_submit("credential@not-a-uuid", {"DEMO_PIN": "x"})
    assert normalize_event(raw, cipher=Cipher(b"\x07" * 32), **KW) is None

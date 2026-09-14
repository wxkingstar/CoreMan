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

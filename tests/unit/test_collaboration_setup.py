from types import SimpleNamespace

import pytest

from coreman.core.chat.collaboration_setup import verified_identity


def event():
    return SimpleNamespace(
        platform="feishu",
        chat_id="group",
        chat_type="group",
        platform_msg_id="mid",
        payload={
            "mentions_bot": True,
            "raw": {
                "header": {
                    "app_id": "receiver",
                    "tenant_key": "tenant",
                    "event_type": "im.message.receive_v1",
                },
                "event": {
                    "sender": {"sender_type": "bot", "sender_id": {"union_id": "sender-union"}},
                    "message": {
                        "message_id": "mid",
                        "chat_id": "group",
                        "chat_type": "group",
                        "message_type": "post",
                        "mentions": [{"id": {"open_id": "receiver-open"}}],
                    },
                },
            },
        },
    )


def test_setup_accepts_exact_real_receipt():
    assert verified_identity(
        event(), message_id="mid", chat_id="group", app_id="receiver", open_id="receiver-open"
    ) == ("tenant", "sender-union")


@pytest.mark.parametrize(
    "field,value",
    [("message_id", "other"), ("chat_id", "other"), ("app_id", "other"), ("open_id", "other")],
)
def test_setup_rejects_wrong_receipt_binding(field, value):
    values = dict(message_id="mid", chat_id="group", app_id="receiver", open_id="receiver-open")
    values[field] = value
    with pytest.raises(ValueError):
        verified_identity(event(), **values)


def test_setup_rejects_user_and_missing_union():
    for sender in [
        {"sender_type": "user", "sender_id": {"union_id": "sender-union"}},
        {"sender_type": "bot", "sender_id": {}},
    ]:
        row = event()
        row.payload["raw"]["event"]["sender"] = sender
        with pytest.raises(ValueError):
            verified_identity(
                row, message_id="mid", chat_id="group", app_id="receiver", open_id="receiver-open"
            )

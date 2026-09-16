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


@pytest.mark.asyncio
async def test_membership_uses_each_bots_own_explicit_token(monkeypatch):
    from coreman.core.chat import collaboration_setup as service

    calls = []

    class Client:
        def __init__(self, app_id, secret):
            self.app_id = app_id

        async def get_token(self):
            return f"token-{self.app_id}"

        async def call(self, method, path, *, token):
            calls.append((self.app_id, method, path, token))
            return {"data": {"is_in_chat": True}}

        async def aclose(self):
            pass

    monkeypatch.setattr(service, "FeishuClient", Client)
    monkeypatch.setattr(
        service, "credentials", lambda bot, _: {"app_id": bot.name, "app_secret": "s"}
    )
    await service.check_current_group(
        SimpleNamespace(name="A"), SimpleNamespace(name="B"), "oc_current", object()
    )
    assert calls == [
        (name, "GET", "/open-apis/im/v1/chats/oc_current/members/is_in_chat", f"token-{name}")
        for name in ("A", "B")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,reason",
    [
        (False, "不在当前群"),
        (None, "无法确认"),
        ("timeout", "无法确认"),
        ("denied", "im:chat.members:read"),
        ("tenant", "不在同一租户"),
    ],
)
async def test_membership_false_is_distinct_from_unverifiable(monkeypatch, outcome, reason):
    from coreman.core.chat import collaboration_setup as service
    from coreman.core.platforms.feishu import FeishuError

    calls = []

    class Client:
        def __init__(self, app_id, secret):
            self.app_id = app_id

        async def get_token(self):
            return self.app_id

        async def call(self, method, path, *, token):
            calls.append(self.app_id)
            if self.app_id == "B":
                if outcome == "timeout":
                    raise TimeoutError
                if outcome == "denied":
                    raise FeishuError(99991672)
                if outcome == "tenant":
                    raise FeishuError(232010)
                return {"data": {"is_in_chat": outcome}}
            return {"data": {"is_in_chat": True}}

        async def aclose(self):
            pass

    monkeypatch.setattr(service, "FeishuClient", Client)
    monkeypatch.setattr(
        service, "credentials", lambda bot, _: {"app_id": bot.name, "app_secret": "s"}
    )
    with pytest.raises(ValueError, match=reason):
        await service.check_current_group(
            SimpleNamespace(name="A"), SimpleNamespace(name="B"), "oc_current", object()
        )
    assert calls == ["A", "B"]

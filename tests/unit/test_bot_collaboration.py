import pytest

from coreman.core.chat.bot_collaboration import event_matches, issue_capability, read_capability
from coreman.core.crypto import Cipher


def test_capability_is_bound_expiring_and_authenticated():
    cipher = Cipher(b"x" * 32)
    token = issue_capability(cipher, task_id=12, user_id="actor", now=100)
    assert read_capability(cipher, token, now=101) == (12, "actor")
    with pytest.raises(ValueError):
        read_capability(cipher, token, now=4000)
    with pytest.raises(ValueError):
        read_capability(Cipher(b"y" * 32), token, now=101)


def test_platform_event_must_match_all_boundaries():
    raw = {
        "header": {"tenant_key": "tenant"},
        "event": {
            "sender": {"sender_type": "bot", "sender_id": {"union_id": "peer"}},
            "message": {
                "message_id": "issued",
                "chat_id": "group",
                "chat_type": "group",
                "message_type": "text",
                "parent_id": "request",
            },
        },
    }
    kwargs = dict(
        tenant="tenant", chat="group", sender_union="peer", message_id="issued", parent_id="request"
    )
    assert event_matches(raw, **kwargs)
    for key in kwargs:
        assert not event_matches(raw, **{**kwargs, key: "wrong"})
    raw["event"]["sender"]["sender_type"] = "user"
    assert not event_matches(raw, **kwargs)


def test_bot_normalization_preserves_bot_sender_and_never_fakes_user():
    import copy

    from coreman.runtime.gateway_feishu.inbound import normalize_event
    from tests.unit.test_feishu_inbound import KW, RAW

    raw = copy.deepcopy(RAW)
    raw["event"]["sender"]["sender_type"] = "bot"
    # Even if a callback carries an ID-shaped field, bots cannot become people.
    assert normalize_event(raw, **KW) is None
    normalized = normalize_event(raw, **KW, allow_bot=True)
    assert normalized.sender.sender_type == "bot"
    assert normalized.sender.platform_user_id == ""
    raw["event"]["message"]["mentions"] = []
    assert normalize_event(raw, **KW, allow_bot=True) is None

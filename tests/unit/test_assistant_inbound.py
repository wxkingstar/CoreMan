"""个人助理应用的事件分流：只接私聊，群消息丢弃，进群即退。"""

import json

from coreman.runtime.gateway_feishu.assistant_inbound import Triage, triage

APP = "cli_example"


def message(chat_type="p2p", sender_type="user", user_id="u_zhang"):
    return {
        "header": {"app_id": APP, "event_type": "im.message.receive_v1", "event_id": "ev1"},
        "event": {
            "sender": {
                "sender_type": sender_type,
                "sender_id": {"user_id": user_id, "open_id": "ou_zhang"},
            },
            "message": {
                "message_id": "om1",
                "chat_id": "oc_p2p" if chat_type == "p2p" else "oc_group",
                "chat_type": chat_type,
                "message_type": "text",
                "content": json.dumps({"text": "你好"}),
                "mentions": [],
            },
        },
    }


def test_private_message_goes_to_its_sender():
    assert triage(message(), app_id=APP) == Triage("private", platform_user_id="u_zhang")


def test_group_messages_are_dropped_before_normalization():
    found = triage(message(chat_type="group"), app_id=APP)
    assert found.kind == "ignore" and found.reason == "group_message"


def test_messages_without_a_human_sender_are_dropped():
    assert triage(message(sender_type="bot"), app_id=APP).reason == "no_sender"
    assert triage(message(user_id=""), app_id=APP).reason == "no_sender"


def test_events_of_another_app_or_unknown_type_are_ignored():
    assert triage(message(), app_id="cli_other").kind == "ignore"
    raw = {"header": {"app_id": APP, "event_type": "im.message.reaction.created_v1"}}
    assert triage(raw, app_id=APP).kind == "ignore"
    assert triage({}, app_id=APP).kind == "ignore"


def test_being_added_to_a_group_means_leave():
    raw = {
        "header": {
            "app_id": APP,
            "event_type": "im.chat.member.bot.added_v1",
            "event_id": "ev_added",
        },
        "event": {"chat_id": "oc_group", "operator_id": {"user_id": "u_li"}},
    }
    assert triage(raw, app_id=APP) == Triage(
        "leave", platform_user_id="u_li", chat_id="oc_group", event_id="ev_added"
    )
    raw["event"]["operator_id"] = {}
    assert triage(raw, app_id=APP).kind == "leave"  # 不知道是谁拉的也要退
    raw["event"]["chat_id"] = ""
    assert triage(raw, app_id=APP).kind == "ignore"


def test_entering_the_private_chat_and_card_clicks_route_by_operator():
    entered = {
        "header": {
            "app_id": APP,
            "event_type": "im.chat.access_event.bot_p2p_chat_entered_v1",
            "event_id": "ev_enter",
        },
        "event": {"chat_id": "oc_p2p", "operator_id": {"user_id": "u_zhang"}},
    }
    assert triage(entered, app_id=APP) == Triage("private", platform_user_id="u_zhang")
    click = {
        "header": {"app_id": APP, "event_type": "card.action.trigger", "event_id": "ev_click"},
        "event": {
            "operator": {"user_id": "u_zhang"},
            "context": {"open_chat_id": "oc_p2p", "open_message_id": "om_card"},
            "action": {"value": {"task_id": "personal:1"}},
        },
    }
    assert triage(click, app_id=APP) == Triage("private", platform_user_id="u_zhang", click=True)
    click["event"]["operator"] = {}
    assert triage(click, app_id=APP).kind == "ignore"

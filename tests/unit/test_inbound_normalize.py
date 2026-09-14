import uuid
from datetime import UTC, datetime

from coreman.core.wecom.messages import message_type_of, text_of
from coreman.runtime.gateway_wecom.inbound import normalize_frame
from coreman.runtime.gateway_wecom.runner import BotInfo

BOT = BotInfo(
    id=uuid.uuid4(),
    bot_key="b",
    name="小助手",
    welcome_message=None,
    wecom_bot_id="wx",
    secret="s",
    credentials_fingerprint="f",
)
NOW = datetime(2026, 9, 11, tzinfo=UTC)


def frame(body: dict, cmd: str = "aibot_msg_callback", req_id: str = "r1") -> dict:
    return {"cmd": cmd, "headers": {"req_id": req_id}, "body": body}


def test_text_single_and_group() -> None:
    m = normalize_frame(
        BOT,
        frame(
            {
                "msgid": "m1",
                "aibotid": "wx",
                "chattype": "single",
                "from": {"userid": "zs"},
                "msgtype": "text",
                "text": {"content": "你好"},
            }
        ),
        gateway_instance="gw",
        now=NOW,
    )
    assert (
        m
        and m.kind == "message"
        and m.chat_type == "single"
        and m.chat_id == "zs"
        and m.sender.platform_user_id == "zs"
    )
    assert (
        text_of(m) == "你好"
        and message_type_of(m) == "text"
        # chat_type / chat_id 也在里面：流被企微判失效后，网关只能靠 reply_context 找到
        # 会话把终稿改走主动推送。
        and m.reply_context
        == {
            "gateway_instance": "gw",
            "req_id": "r1",
            "received_at": NOW.isoformat(),
            "chat_type": "single",
            "chat_id": "zs",
        }
    )
    g = normalize_frame(
        BOT,
        frame(
            {
                "msgid": "m2",
                "chattype": "group",
                "chatid": "g1",
                "from": {"userid": "zs"},
                "msgtype": "text",
                "text": {"content": "@小助手 hi"},
            }
        ),
        gateway_instance="gw",
        now=NOW,
    )
    assert g and g.chat_type == "group" and g.chat_id == "g1" and g.mentions_bot
    bad = normalize_frame(
        BOT,
        frame(
            {
                "msgid": "m3",
                "chattype": "weird",
                "from": {"userid": "zs"},
                "msgtype": "text",
                "text": {"content": "x"},
            }
        ),
        gateway_instance="gw",
        now=NOW,
    )
    assert bad and bad.chat_type == "single"


def test_media_voice_quote_and_events() -> None:
    img = normalize_frame(
        BOT,
        frame(
            {
                "msgid": "m4",
                "chattype": "single",
                "from": {"userid": "zs"},
                "msgtype": "image",
                "image": {"url": "u", "aeskey": "k"},
            }
        ),
        gateway_instance="gw",
        now=NOW,
    )
    assert img and message_type_of(img) == "image" and img.parts[0].type == "image"
    mixed = normalize_frame(
        BOT,
        frame(
            {
                "msgid": "m5",
                "chattype": "single",
                "from": {"userid": "zs"},
                "msgtype": "mixed",
                "mixed": {
                    "msg_item": [
                        {"msgtype": "text", "text": {"content": "看图"}},
                        {"msgtype": "image", "image": {"url": "u", "aeskey": "k"}},
                    ]
                },
            }
        ),
        gateway_instance="gw",
        now=NOW,
    )
    assert mixed and message_type_of(mixed) == "mixed" and text_of(mixed) == "看图"
    voice = normalize_frame(
        BOT,
        frame(
            {
                "msgid": "m6",
                "chattype": "single",
                "from": {"userid": "zs"},
                "msgtype": "voice",
                "voice": {"content": "转写文本"},
            }
        ),
        gateway_instance="gw",
        now=NOW,
    )
    assert voice and text_of(voice) == "转写文本" and message_type_of(voice) == "voice"
    quoted = normalize_frame(
        BOT,
        frame(
            {
                "msgid": "m7",
                "chattype": "single",
                "from": {"userid": "zs"},
                "msgtype": "text",
                "text": {"content": "这个呢"},
                "quote": {"msgtype": "image", "image": {"url": "u", "aeskey": "k"}},
            }
        ),
        gateway_instance="gw",
        now=NOW,
    )
    assert quoted and message_type_of(quoted) == "quote_image"
    enter = normalize_frame(
        BOT,
        frame(
            {
                "msgid": "e1",
                "chattype": "single",
                "from": {"userid": "zs"},
                "msgtype": "event",
                "event": {"eventtype": "enter_chat"},
            },
            cmd="aibot_event_callback",
        ),
        gateway_instance="gw",
        now=NOW,
    )
    assert enter and enter.kind == "enter_chat"
    assert (
        normalize_frame(
            BOT, {"cmd": "unknown", "headers": {}, "body": {}}, gateway_instance="gw", now=NOW
        )
        is None
    )


def test_template_card_event_becomes_card_action() -> None:
    frame = {
        "cmd": "aibot_event_callback",
        "headers": {"req_id": "r9"},
        "body": {
            "msgid": "e1",
            "aibotid": "bot1",
            "chattype": "group",
            "chatid": "g1",
            "from": {"userid": "zs"},
            "msgtype": "event",
            "event": {
                "eventtype": "template_card_event",
                "template_card_event": {
                    "card_type": "vote_interaction",
                    "event_key": "submit_choice",
                    "task_id": "choice@b@zs@1@0",
                    "selected_items": {
                        "selected_item": [
                            {
                                "question_key": "choice_answer",
                                "option_ids": {"option_id": ["opt_1"]},
                            }
                        ]
                    },
                },
            },
        },
    }
    m = normalize_frame(BOT, frame, gateway_instance="gw", now=NOW)
    assert m and m.kind == "card_action" and m.chat_id == "g1" and m.reply_context["req_id"] == "r9"
    assert m.card_action == {
        "task_id": "choice@b@zs@1@0",
        "card_type": "vote_interaction",
        "event_key": "submit_choice",
        "selected": {"choice_answer": ["opt_1"]},
    }
    bad = {
        **frame,
        "body": {
            **frame["body"],
            "event": {"eventtype": "template_card_event", "template_card_event": {}},
        },
    }
    assert normalize_frame(BOT, bad, gateway_instance="gw", now=NOW) is None

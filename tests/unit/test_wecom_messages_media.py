from datetime import UTC, datetime
from typing import Any

from coreman.core.wecom.messages import (
    CardAction,
    FilePart,
    ImagePart,
    QuotePart,
    TextPart,
    VideoPart,
    message_type_of,
    parse_card_event,
)
from coreman.runtime.gateway_wecom.inbound import normalize_frame
from coreman.runtime.gateway_wecom.runner import BotInfo
from tests.unit.test_inbound_normalize import BOT, NOW  # 既有用例里的 BotInfo/时间常量


def _frame(body: dict[str, Any]) -> dict[str, Any]:
    return {"cmd": "aibot_msg_callback", "headers": {"req_id": "r1"}, "body": body}


def _body(msgtype: str, payload: dict[str, Any], **extra: object) -> dict[str, Any]:
    return {
        "msgid": "m1",
        "aibotid": "bot1",
        "chattype": "single",
        "from": {"userid": "zs"},
        "msgtype": msgtype,
        msgtype: payload,
        **extra,
    }


def test_image_file_video_refs_are_kept_not_downloaded() -> None:
    img = normalize_frame(
        BOT,
        _frame(_body("image", {"url": "https://x/1", "aeskey": "k"})),
        gateway_instance="gw",
        now=NOW,
    )
    assert img and img.parts == [ImagePart(ref={"url": "https://x/1", "aeskey": "k"})]
    f = normalize_frame(
        BOT,
        _frame(_body("file", {"url": "https://x/2", "aeskey": "k"})),
        gateway_instance="gw",
        now=NOW,
    )
    assert f and f.parts == [FilePart(ref={"url": "https://x/2", "aeskey": "k"}, filename=None)]
    v = normalize_frame(
        BOT,
        _frame(_body("video", {"url": "https://x/3", "aeskey": "k"})),
        gateway_instance="gw",
        now=NOW,
    )
    assert v and v.parts == [VideoPart(ref={"url": "https://x/3", "aeskey": "k"})]
    assert message_type_of(v) == "video"


def test_mixed_and_quote_shapes() -> None:
    body = _body(
        "mixed",
        {
            "msg_item": [
                {"msgtype": "text", "text": {"content": "看图"}},
                {"msgtype": "image", "image": {"url": "u", "aeskey": "k"}},
            ]
        },
        quote={"msgtype": "file", "file": {"url": "qu", "aeskey": "qk"}},
    )
    m = normalize_frame(BOT, _frame(body), gateway_instance="gw", now=NOW)
    assert m and m.parts[:2] == [TextPart(text="看图"), ImagePart(ref={"url": "u", "aeskey": "k"})]
    assert m.parts[2] == QuotePart(kind="file", text=None, refs=[{"url": "qu", "aeskey": "qk"}])
    assert message_type_of(m) == "quote_file"
    body2 = _body(
        "text",
        {"content": "回复"},
        quote={
            "msgtype": "mixed",
            "mixed": {"msg_item": [{"msgtype": "text", "text": {"content": "原文"}}]},
        },
    )
    q = normalize_frame(BOT, _frame(body2), gateway_instance="gw", now=NOW)
    assert q and q.parts[1] == QuotePart(
        kind="mixed",
        text=None,
        refs=[{"msg_item": [{"msgtype": "text", "text": {"content": "原文"}}]}],
    )


def test_parse_card_event() -> None:
    ev = {
        "eventtype": "template_card_event",
        "template_card_event": {
            "card_type": "vote_interaction",
            "event_key": "submit_choice",
            "task_id": "choice@b@u@1@0",
            "selected_items": {
                "selected_item": [
                    {
                        "question_key": "choice_answer",
                        "option_ids": {"option_id": ["opt_0", "opt_other"]},
                    }
                ]
            },
        },
    }
    assert parse_card_event(ev) == CardAction(
        task_id="choice@b@u@1@0",
        card_type="vote_interaction",
        event_key="submit_choice",
        selected={"choice_answer": ["opt_0", "opt_other"]},
    )
    assert parse_card_event({"eventtype": "template_card_event", "template_card_event": {}}) is None
    assert parse_card_event({"eventtype": "feedback_event"}) is None
    assert isinstance(BotInfo, type) and datetime.now(UTC)

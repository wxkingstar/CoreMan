import json
from datetime import UTC, datetime
from uuid import uuid4

from coreman.core.wecom.cards import choice_card, question_task_id
from coreman.runtime.gateway_feishu.cards import interaction_card, split_utf8, stream_card
from coreman.runtime.gateway_feishu.inbound import normalize_event


def test_card_size_budget_counts_json_escapes_and_preserves_every_character():
    value = '中文🎉\n"\\\x00' * 10000
    chunks = split_utf8(value)
    assert "".join(chunks) == value
    for chunk in chunks:
        card = stream_card("思考" * 10000, chunk)
        assert len(json.dumps(card, ensure_ascii=False).encode()) < 30_000


def test_multiple_choice_form_roundtrips_current_interaction_keys():
    task_id = question_task_id("choice@bot@user@rnd", 0)
    original = choice_card(
        {"question": "选择", "multiSelect": True, "options": [{"label": "A"}, {"label": "B"}]},
        index=0,
        total=1,
        task_id=task_id,
        icon_url="",
    )
    card = interaction_card(original)
    form = card["body"]["elements"][1]
    assert form["elements"][0]["tag"] == "multi_select_static"
    button = form["elements"][1]
    action = {
        "value": button["behaviors"][0]["value"],
        "form_value": {"choice_answer": ["opt_0", "opt_1"]},
    }
    raw = {
        "header": {"app_id": "cli", "event_type": "card.action.trigger", "event_id": "event1"},
        "event": {
            "operator": {"user_id": "employee"},
            "context": {"open_chat_id": "oc1", "open_message_id": "om1"},
            "action": action,
        },
    }
    message = normalize_event(
        raw,
        bot_id=uuid4(),
        app_id="cli",
        bot_open_id="ou_bot",
        gateway_instance="gateway",
        now=datetime.now(UTC),
    )
    assert message is not None
    assert message.card_action["task_id"] == task_id
    assert message.card_action["selected"] == {"choice_answer": ["opt_0", "opt_1"]}
    assert message.card_action["event_key"] == "submit_choice"

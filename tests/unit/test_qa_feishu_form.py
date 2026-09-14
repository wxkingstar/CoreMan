"""Live Feishu QA: JSON 2.0 rejected the old form button with code 300123."""
from coreman.core.wecom.cards import choice_card
from coreman.runtime.gateway_feishu.cards import interaction_card


def test_json2_question_has_a_valid_submit_button():
    original = choice_card(
        {"question": "Color?", "options": [{"label": "RED"}, {"label": "GREEN"}]},
        index=0, total=1, task_id="choice@test@user@1@0", icon_url="",
    )
    form = interaction_card(original)["body"]["elements"][1]
    submit = form["elements"][-1]
    assert submit["form_action_type"] == "submit"
    assert "action_type" not in submit
    assert submit["behaviors"][0]["value"]["task_id"] == original["task_id"]

"""网关应答前认出卡片按钮点的是什么，用来选 toast 文案。"""

import uuid
from typing import Any

import pytest

from coreman.core.wecom.messages import InboundMessage, Sender
from coreman.runtime.gateway_feishu import reply_buttons as rb


def _click(**action: Any) -> InboundMessage:
    return InboundMessage(
        platform="feishu",
        bot_id=uuid.uuid4(),
        kind="card_action",
        chat_type="single",
        chat_id="oc_1",
        sender=Sender(platform_user_id="u1"),
        message_id="action:e1",
        card_action={"task_id": "personal:1", "card_type": "form", **action},
    )


def test_event_keys_match_the_cards_that_send_them() -> None:
    from coreman.core.assistants import cold_start
    from coreman.core.assistants.approvals import batch
    from coreman.core.chat import topics
    from coreman.core.cloud_proxy import confirm as cloud
    from coreman.core.feishu_docs import confirm as docs
    from coreman.core.feishu_personal import inflow
    from coreman.core.systems_catalog import confirm as systems
    from coreman.runtime.worker import assistant_stop

    assert {docs.CARD_EVENT, batch.CARD_EVENT, cloud.CARD_EVENT, systems.CARD_EVENT} == (
        rb.WRITE_CARD_EVENTS
    )
    assert systems.RESEND == "resend"
    assert set(rb.TOPIC_EVENTS) == {topics.NEW, topics.FORK}
    assert cold_start.EVENT_KEY == rb.COLD_START_EVENT
    assert assistant_stop.EVENT_KEY == rb.STOP_EVENT
    assert inflow.UPGRADE_EVENT in rb.AUTHORIZE_EVENTS


def test_schedule_event_matches() -> None:
    from coreman.core import personal_schedules

    assert personal_schedules.CARD_EVENT == rb.SCHEDULE_EVENT


def test_choice_card_keys_match() -> None:
    from coreman.core.wecom import cards

    choice = cards.choice_card(
        {"header": "h", "question": "q", "options": [{"label": "a"}]},
        index=0,
        total=1,
        task_id="t",
        icon_url="",
    )
    assert choice["submit_button"]["key"] == rb.CHOICE_SUBMIT
    assert rb.CHOICE_OTHER in {o["id"] for o in choice["checkbox"]["option_list"]}
    switch = cards.switch_offer_card(
        task_id="t", current_name="a", target_name="b", pct_text="90%", icon_url=""
    )
    assert switch["submit_button"]["key"] == rb.SWITCH_SUBMIT
    assert {o["id"] for o in switch["checkbox"]["option_list"]} == set(rb.SWITCH_OPTIONS)


@pytest.mark.parametrize(
    ("action", "outcome"),
    [
        ({"event_key": "cloud_write", "level": "confirm:abc"}, "write_confirm"),
        ({"event_key": "feishu_group_write", "level": "cancel:abc"}, "write_cancel"),
        ({"event_key": "systems_write", "level": "resend:abc"}, "write_resend"),
        ({"event_key": "personal_schedule", "level": "confirm:abc"}, "schedule_confirm"),
        ({"event_key": "personal_schedule", "level": "cancel:abc"}, "schedule_cancel"),
        ({"event_key": "topic_new"}, "topic_new"),
        ({"event_key": "topic_fork"}, "topic_fork"),
        ({"event_key": "assistant_cold_start_card", "level": "role.answer.x"}, "cold_start_answer"),
        ({"event_key": "assistant_cold_start_card", "level": "role.skip.x"}, "cold_start_skip"),
        ({"event_key": "assistant_stop", "level": "1.x"}, "assistant_stop"),
        ({"card_type": "credential"}, "credential_submit"),
        ({"event_key": "personal_authorize", "level": "all"}, "authorize_link"),
        ({"event_key": "personal_upgrade", "level": "w1"}, "authorize_link"),
        (
            {"event_key": "submit_choice", "selected": {"choice_answer": ["opt_0"]}},
            "choice_submit",
        ),
        (
            {"event_key": "submit_choice", "selected": {"choice_answer": ["opt_other"]}},
            "choice_other",
        ),
        (
            {
                "event_key": "ratelimit_switch_submit",
                "selected": {"ratelimit_switch_choice": ["opt_switch"]},
            },
            "switch_runtime",
        ),
        (
            {
                "event_key": "ratelimit_switch_submit",
                "selected": {"ratelimit_switch_choice": ["opt_wait"]},
            },
            "switch_wait",
        ),
        ({"event_key": "cloud_write", "level": "odd:abc"}, None),
        ({"event_key": "something_new"}, None),
    ],
)
def test_card_click_names_the_button(action: dict[str, Any], outcome: str | None) -> None:
    assert rb._card_click(_click(**action)) == outcome

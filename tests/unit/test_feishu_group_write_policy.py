from coreman.core.feishu_docs import write_policy as wp
from coreman.core.feishu_personal import endpoints
from coreman.core.feishu_personal.tools import TOOLS


def test_every_listed_tool_exists_and_no_private_domains():
    assert set(wp.GROUP_TOOLS) <= set(TOOLS)
    for name, category in wp.GROUP_TOOLS.items():
        # 发消息类端点只能是 confirm（当前仅 feishu_share_document，spec 明确列为 confirm）
        if endpoints.ENDPOINTS[TOOLS[name].endpoint].kind == "send":
            assert category == "confirm"
        banned = ("mail", "message", "approval", "okr", "attendance")
        assert not any(word in name for word in banned)


def test_target_fields_match_argument_models():
    for name, field in wp.TARGET_FIELD.items():
        assert wp.GROUP_TOOLS[name] == "target"
        assert field in TOOLS[name].args.model_fields
    assert {n for n, c in wp.GROUP_TOOLS.items() if c == "target"} == set(wp.TARGET_FIELD)


def test_inviting_others_needs_confirmation():
    class Args:
        attendee_open_ids = ["ou_x"]

    assert wp.effective_category("feishu_calendar_create_event", Args()) == "confirm"
    Args.attendee_open_ids = []
    assert wp.effective_category("feishu_calendar_create_event", Args()) == "create"


def test_locate_results_keep_only_locating_fields():
    out = wp.trim_locate(
        {
            "items": [
                {"event_id": "e", "summary": "周会", "description": "机密", "attendees": ["a"]}
            ],
            "has_more": False,
        }
    )
    assert out == {"items": [{"event_id": "e", "summary": "周会"}], "has_more": False}


def test_args_hash_ignores_key_order():
    assert wp.args_hash({"a": 1, "b": 2}) == wp.args_hash({"b": 2, "a": 1})

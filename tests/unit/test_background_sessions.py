"""Claude 后台阶段的纯函数：帧解析、请求字段、快照合并、卡片区块、标题与提示词规则。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from coreman.core.chat import background_runs, liveness
from coreman.core.chat.busy_choice import BG_STOP, BG_WAIT, background_offer_card
from coreman.core.chat.stream_stop import CONFIRMED, confirm_card
from coreman.core.prompting.defaults import BACKGROUND_TASKS_RULE
from coreman.core.prompting.system_prompt import build_system_prompt
from coreman.core.relay.client import ChatRequest
from coreman.core.relay.sse import (
    BackgroundEndEvent,
    BackgroundEvent,
    BackgroundTasksEvent,
    LiveJoinedEvent,
    SseParser,
    TurnEndEvent,
)
from coreman.runtime.gateway_feishu.cards import BACKGROUND_PANEL_ID, background_panel


def task(tid: str, status: str = "running", **extra: object) -> dict[str, object]:
    return {
        "id": tid,
        "description": f"任务 {tid}",
        "kind": "agent",
        "status": status,
        "started_at": 1795425877,
        **extra,
    }


def frame(runtime: dict[str, object]) -> str:
    return "data: " + json.dumps({"x_runtime": runtime, "choices": []}, ensure_ascii=False)


def test_parser_turns_runtime_frames_into_events() -> None:
    parser = SseParser("claude")
    [bg] = parser.feed_line(frame({"background": {"tasks": [task("a")], "deadline": 1795430000}}))
    assert isinstance(bg, BackgroundEvent) and bg.deadline == 1795430000
    assert bg.tasks[0]["id"] == "a" and bg.tasks[0]["kind"] == "agent"
    [snap] = parser.feed_line(frame({"background_tasks": [task("a", "completed"), {"id": 3}]}))
    assert isinstance(snap, BackgroundTasksEvent) and [t["status"] for t in snap.tasks] == [
        "completed"
    ]
    [joined] = parser.feed_line(frame({"live_joined": {"tasks": [task("b")]}}))
    assert isinstance(joined, LiveJoinedEvent) and joined.tasks[0]["id"] == "b"
    assert parser.feed_line(frame({"turn_end": {"merged": True}})) == [TurnEndEvent(True)]
    assert parser.feed_line(frame({"turn_end": {}})) == [TurnEndEvent(False)]
    [end] = parser.feed_line(frame({"background_end": {"state": "weird", "tasks": []}}))
    assert end == BackgroundEndEvent("failed", ())
    # 不计入「零事件」判定：它们不是模型的输出。
    assert parser.counts == {"text": 0, "thinking": 0, "tool": 0, "usage": 0}


def test_task_fields_are_bounded() -> None:
    [item] = background_runs_tasks([task("a", description="x" * 500, last_progress="y\n" * 90)])
    assert len(item["description"]) == 120 and len(item["last_progress"]) <= 80
    assert "\n" not in item["last_progress"]


def background_runs_tasks(value: object) -> tuple[dict[str, object], ...]:
    from coreman.core.relay.sse import background_tasks

    return background_tasks(value)


def _request(**extra: object) -> ChatRequest:
    return ChatRequest(
        model="m",
        system_prompt="s",
        user_content="hi",
        working_dir="/w",
        session_id="sid",
        backend=str(extra.pop("backend", "claude")),
        **extra,  # type: ignore[arg-type]
    )


def test_live_fields_are_sent_only_for_claude_with_an_owner() -> None:
    body = _request(live_owner="u1", live_deadline=1795430000).to_body()
    assert body["live_owner"] == "u1" and body["live_deadline"] == 1795430000
    assert "live_join" not in body
    assert _request(live_owner="u1", live_deadline=1, live_join=True).to_body()["live_join"]
    assert "live_owner" not in _request().to_body()
    assert "live_owner" not in _request(backend="codex", live_owner="u1").to_body()


def test_merge_keeps_order_and_finished_since_numbers_items() -> None:
    old = [task("a"), task("b")]
    new = [task("b", "completed"), task("c")]
    merged = background_runs.merge(old, new)
    assert [t["id"] for t in merged] == ["a", "b", "c"]
    assert merged[1]["status"] == "completed"
    assert [(i, t["id"]) for i, t in background_runs.finished_since(old, new)] == [(2, "b")]
    # 已经结束过的不再算一次。
    assert background_runs.finished_since(merged, [task("b", "completed")]) == []


def test_task_line_shows_progress_and_stall() -> None:
    now = datetime.fromtimestamp(1795425877 + 12 * 60, UTC)
    line = background_runs.task_line(
        1, task("a", last_progress="跑后端测试", last_progress_at=1795425877 + 11 * 60), now
    )
    assert line == "① 任务 a　进行中 12 分钟 · 最近：跑后端测试"
    stalled = background_runs.task_line(2, task("b", last_progress="x"), now)
    assert stalled.endswith("好久没有进展")
    assert background_runs.task_line(3, task("c", "completed"), now).endswith("已完成 ✓")


def test_panel_has_stop_buttons_only_while_running() -> None:
    view = {
        "run_id": 7,
        "state": "running",
        "deadline": 1795430000,
        "tasks": [task("a"), task("b", "completed")],
    }
    panel = background_panel(view, now=1795425877 + 60)
    assert panel["element_id"] == BACKGROUND_PANEL_ID
    assert "1 项进行中" in panel["header"]["title"]["content"]
    dumped = json.dumps(panel, ensure_ascii=False)
    values = [
        b["behaviors"][0]["value"]
        for b in _buttons(panel)
        if b["behaviors"][0]["value"]["task_id"] == "bg@7"
    ]
    assert [v["level"] for v in values] == ["a", background_runs.ALL]
    assert "结果已在下方回复" in dumped
    finished = background_panel({**view, "state": "finished"}, now=1795425877 + 60)
    assert _buttons(finished) == []
    assert "全部完成" in finished["header"]["title"]["content"]


def _buttons(element: object) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    if isinstance(element, dict):
        if element.get("tag") == "button":
            found.append(element)
        for value in element.values():
            found += _buttons(value)
    elif isinstance(element, list):
        for value in element:
            found += _buttons(value)
    return found


def test_final_heading_follows_the_background_state() -> None:
    running = {"background": {"state": "running", "tasks": [task("a"), task("b")]}}
    assert liveness.final_heading(running) == "⏳ 已开工 · 后台 2 项进行中"
    done = {"background": {"state": "finished", "tasks": []}}
    assert liveness.final_heading(done) == "✅ 后台全部完成"
    # 进后台阶段之前只有快照、没有 state：照常。
    plain = {"background": {"tasks": [task("a")]}}
    assert liveness.final_heading(plain) == liveness.DONE_HEADING


def test_card_ids_round_trip() -> None:
    assert background_runs.parse_card_task_id(background_runs.card_task_id(42)) == 42
    assert background_runs.parse_card_task_id("bg@0") is None
    offer = background_offer_card("feishu", 9, running=2)
    keys = [b["behaviors"][0]["value"]["event_key"] for b in _buttons(offer)]
    assert keys == [BG_WAIT, BG_STOP]
    [ok] = _buttons(confirm_card(9, 3))
    assert ok["behaviors"][0]["value"]["level"] == CONFIRMED


def _prompt(**extra: object) -> str:
    return build_system_prompt(
        segments=_segments(),
        backend=str(extra.pop("backend", "claude")),
        verbosity_level=4,
        bot_prompt="",
        tag="t",
        **extra,  # type: ignore[arg-type]
    )


def _segments():  # type: ignore[no-untyped-def]
    from coreman.core.prompting.system_prompt import PromptSegments

    return PromptSegments("sec", "contract", "mode", "tail", {1: "v1", 2: "v2", 3: "v3"})


def test_prompt_rules_only_when_the_node_supports_background_sessions() -> None:
    assert BACKGROUND_TASKS_RULE in _prompt(background_tasks=True)
    assert BACKGROUND_TASKS_RULE not in _prompt()
    assert BACKGROUND_TASKS_RULE not in _prompt(backend="codex", background_tasks=True)

"""长任务卡片状态行：按看的人时区显示的时间、静默、已知长等待、疑似卡住、中断。"""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from coreman.core.chat import liveness, redaction
from coreman.core.relay.sse import SseParser, ToolUseStart
from coreman.core.wecom.thinking import ThinkingCollector
from coreman.runtime.worker import stream_writer
from coreman.runtime.worker.stream_writer import StreamWriter

# UTC 2026-11-11 09:20
START = datetime(2026, 11, 11, 9, 20, tzinfo=UTC)


def at(moment: datetime, format_type: str = "time") -> str:
    """飞书按看的人设备时区渲染的时间标签。"""
    ms = int(moment.timestamp()) * 1000
    return f"<local_datetime millisecond='{ms}' format_type='{format_type}'></local_datetime>"


T0 = at(START)


def test_running_line_uses_viewer_local_times() -> None:
    activity = {"at": liveness.iso(START + timedelta(minutes=11)), "label": "运行命令：跑测试"}
    line = liveness.heading(START, activity, START + timedelta(minutes=12))
    last = at(START + timedelta(minutes=11))
    assert line == f"⏳ 进行中 · {T0} 开始 · 最后动作 {last}（运行命令：跑测试）"
    # 不再写死任何时区的钟点。
    assert "17:20" not in line and "09:20" not in line


def test_no_action_yet_only_shows_start() -> None:
    assert liveness.heading(START, {}, START) == f"⏳ 进行中 · {T0} 开始"


def test_quiet_after_threshold() -> None:
    activity = {"at": liveness.iso(START), "label": "思考"}
    now = START + timedelta(seconds=liveness.QUIET_SECONDS)
    assert liveness.heading(START, activity, now) == f"⚠️ {T0} 后没有新动作（思考）"
    assert liveness.heading(START, activity, now - timedelta(seconds=1)).startswith("⏳")


def test_long_ago_shows_date() -> None:
    activity = {"at": liveness.iso(START), "label": "思考"}
    near = liveness.heading(START, activity, START + liveness.DATE_AFTER - timedelta(seconds=1))
    assert near.startswith(f"⚠️ {T0} 后")
    far = liveness.heading(START, activity, START + timedelta(days=1))
    assert far.startswith(f"⚠️ {at(START, 'date_short')} {T0} 后")


def test_times_within_a_minute_give_the_same_line() -> None:
    """截到整分钟：同一分钟里的新事件不让状态行变，不多改卡片。"""
    now = START + timedelta(minutes=2)
    first = {"at": liveness.iso(START + timedelta(seconds=5)), "label": "思考"}
    later = {"at": liveness.iso(START + timedelta(seconds=55)), "label": "思考"}
    assert liveness.heading(START, first, now) == liveness.heading(START, later, now)
    assert T0 in liveness.heading(START, first, now)


def test_label_is_escaped_for_markdown() -> None:
    activity = {"at": liveness.iso(START), "label": "运行命令：rm *.log <at id=all></at>"}
    line = liveness.heading(START, activity, START)
    assert "<at" not in line and "*" not in line
    assert "rm &#42;&#46;log &#60;at id&#61;all&#62;&#60;&#47;at&#62;" in line


def test_known_long_wait_holds_quiet_until_expected_end() -> None:
    activity, line = liveness.tool_activity("Bash", "", "gh run watch 42 --exit-status", START)
    assert line == "**运行命令**：等 GitHub Actions 跑完（通常 5–10 分钟）"
    during = liveness.heading(START, activity, START + timedelta(minutes=9))
    assert during == (
        f"⏳ 进行中 · {T0} 开始 · 最后动作 {T0}（运行命令：等 GitHub Actions 跑完）"
        f" · 通常 5–10 分钟 · 预计 {at(START + timedelta(minutes=10))} 前"
    )
    after = liveness.heading(START, activity, START + timedelta(minutes=10))
    assert after.startswith(f"⚠️ {T0} 后没有新动作")


@pytest.mark.parametrize(
    ("command", "what", "high"),
    [
        ("gh pr checks 12 --watch --interval 30", "等 PR 检查跑完", 600),
        ("kubectl rollout status deploy/app", "等部署滚动完成", 300),
        ("sleep 300 && gh pr view", "等待 5 分钟", 300),
        ("sleep 2m", "等待 2 分钟", 120),
        ("sleep 120; gh run watch 1", "等 GitHub Actions 跑完", 600),
    ],
)
def test_long_wait_rules(command: str, what: str, high: int) -> None:
    wait = liveness.long_wait(command)
    assert wait is not None and wait.what == what and wait.high == high


@pytest.mark.parametrize("command", ["", "sleep 5", "gh pr checks 12", "ls -la"])
def test_not_long_waits(command: str) -> None:
    assert liveness.long_wait(command) is None


def test_human_summary_wins_but_command_echo_does_not() -> None:
    _, line = liveness.tool_activity("Bash", "等 CI 跑完", "gh run watch 1", START)
    assert line == "**运行命令**：等 CI 跑完（通常 5–10 分钟）"
    # Codex 节点拿命令本身当 summary：认得出的长等待改用规则里的说法。
    _, line = liveness.tool_activity(
        "shell", "gh run watch 1", "/bin/bash -lc 'gh run watch 1'", START
    )
    assert line.startswith("**运行命令**：等 GitHub Actions 跑完")
    # 老节点什么都没给：只写工具名。
    activity, line = liveness.tool_activity("Read", "", "", START)
    assert line == "**Read**" and activity["label"] == "读文件"


def test_stuck_and_interrupted_headings() -> None:
    activity = {"at": liveness.iso(START), "label": "运行命令", "stuck": True}
    assert liveness.heading(START, activity, START + timedelta(minutes=20)) == (
        f"⚠️ 疑似卡住：{T0} 后没有任何输出（运行命令）"
    )
    done = liveness.interrupted_activity(activity, liveness.REASON_RELAY_SILENT)
    assert "stuck" not in done
    assert liveness.final_heading(done) == "❌ 已中断：运行节点 2 分钟没有响应"
    assert liveness.final_heading({"at": "x"}) == liveness.DONE_HEADING


def test_interrupt_reasons() -> None:
    assert liveness.interrupt_reason("RelayError", "RelayError: SSE timeout: 120 秒无字节") == (
        liveness.REASON_RELAY_SILENT
    )
    assert liveness.interrupt_reason("RelayError", "RelayError: 连接失败") is None
    assert liveness.interrupt_reason("run_interrupted", None) == liveness.REASON_RELAY_INTERRUPTED
    assert liveness.interrupt_reason("hard_ttl", None) == liveness.REASON_HARD_TTL
    assert liveness.interrupt_reason("user_stop", None) is None


def _chunk(call: dict) -> str:
    delta = {"role": "assistant", "content": "", "tool_calls": [call]}
    return "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": None}]})


def test_sse_tool_start_carries_summary_and_command() -> None:
    p = SseParser("claude")
    call = {
        "id": "t1",
        "type": "function",
        "function": {"name": "Bash", "arguments": json.dumps({"command": "gh run watch 9"})},
        "x_summary": "等 CI 跑完\n第二行",
    }
    assert p.feed_line(_chunk(call)) == [ToolUseStart("Bash", "t1", "等 CI 跑完", "gh run watch 9")]


def test_sse_old_nodes_fall_back_to_arguments() -> None:
    p = SseParser("opencode")
    args = json.dumps({"command": "sleep 300", "description": "等一会"})
    call = {"id": "t2", "function": {"name": "bash", "arguments": args}}
    assert p.feed_line(_chunk(call)) == [ToolUseStart("bash", "t2", "等一会", "sleep 300")]
    # Claude 老节点：参数是空串，只有工具名。
    call = {"id": "t3", "function": {"name": "Bash", "arguments": ""}}
    assert p.feed_line(_chunk(call)) == [ToolUseStart("Bash", "t3")]


def test_thinking_collector_keeps_old_line_without_detail() -> None:
    c = ThinkingCollector(clock=lambda: 0.0)
    c.add_tool_call("Bash")
    c.add_tool_call("Bash", "**运行命令**：等 CI")
    assert c.to_markdown() == "🔧 **Bash**\n🔧 **运行命令**：等 CI"


def _writer(secrets: frozenset[str] = frozenset()) -> StreamWriter:
    ctx = SimpleNamespace(
        clock=lambda: 0.0,
        redact=lambda text: redaction.redact(text, secrets),
        redact_text=lambda text: redaction.redact_text(text, secrets),
    )
    return StreamWriter(ctx)  # type: ignore[arg-type]


def test_writer_tracks_last_action_and_redacts(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [START]
    monkeypatch.setattr(stream_writer, "_now", lambda: now[0])
    w = _writer(frozenset({"tok-secret-123456"}))
    w.add_tool("Bash", "推送 tok-secret-123456", "git push")
    assert "tok-secret-123456" not in w.activity["label"]
    assert "tok-secret-123456" not in w.thinking_md()
    w.add_tool("Bash", "", "gh run watch 3")
    assert "until" in w.activity
    # 节点把工具参数回显成思考：同一个动作，不冲掉「在等什么」。
    w.add_thinking("\n\n工具参数 · Bash\n{}\n")
    assert w.activity["label"] == "运行命令：等 GitHub Actions 跑完"
    w.add_thinking("嗯")
    assert w.activity["label"] == "思考" and "until" not in w.activity
    w.add_text("好")
    assert w.activity["label"] == "写回复"


def test_writer_marks_stuck_once_outside_known_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [START]
    monkeypatch.setattr(stream_writer, "_now", lambda: now[0])
    w = _writer()
    assert not w.check_stuck()  # 还没有任何动作
    w.add_tool("Bash", "", "sleep 3600")
    now[0] = START + timedelta(seconds=liveness.STUCK_SECONDS)
    assert not w.check_stuck()  # 还在预计的一小时等待里
    now[0] = START + timedelta(hours=1)
    assert w.check_stuck()  # 超出预计、也过了卡住阈值
    assert not w.check_stuck()  # 只标一次
    w.add_text("回来了")
    assert "stuck" not in w.activity

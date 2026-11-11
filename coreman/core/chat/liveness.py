"""长任务卡片的「死活」信号：开始时间、最后动作、已知的长等待、静默、疑似卡住与中断。

数据全部来自平台自己看得到的事件（worker 收到的 SSE 帧、收尸与恢复的终态），运行中不调用
模型、不定时刷新卡片：网关每轮推送时按 `task_streams.activity` 算出状态行，和上次推上去的
不一样才改卡片（时间只精确到分钟，所以一个任务通常每分钟最多改一次，静默时只多改一两次）。

`activity` 的形状（缺省都可以没有）::

    {"at": ISO 时间, "label": "运行命令：等 CI 跑完",
     "until": ISO 时间, "expect": "通常 5–10 分钟",   # 已知长等待：预计结束前不报静默
     "stuck": true,                                   # worker 判定命令行长时间没有任何输出
     "interrupted": "运行节点 2 分钟没有响应"}          # 终态：卡片标红并在群里 @ 提问人

时间按办公时区（北京时间）显示，和提示词里「用户说的时间按北京时间理解」同一约定。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

DISPLAY_TZ = ZoneInfo("Asia/Shanghai")
# 这么久没有新动作，状态行改成 ⚠️（已知长等待在预计结束前不算）。
QUIET_SECONDS = 5 * 60
# 命令行这么久一个事件都没有、又不在已知长等待里：状态行标「疑似卡住」，只提示不中断。
STUCK_SECONDS = 15 * 60
# 状态行里动作说明的长度上限：面板标题在手机上一行放不下太长的字。
LABEL_CHARS = 36
SUMMARY_CHARS = 80
COMMAND_CHARS = 400

DONE_HEADING = "🤔 思考过程"
INTERRUPTED_PREFIX = "❌ 已中断："

# 中断原因：和已有的终态一一对应。
REASON_WORKER_LOST = "处理进程失联"
REASON_RELAY_SILENT = "运行节点 2 分钟没有响应"
REASON_RELAY_INTERRUPTED = "运行节点连接中断"
REASON_HARD_TTL = "超过单次最长运行时间"
REASON_RECOVERED = "处理没有正常结束"

# 运行中卡片上的「看看进度」按钮。
LIVE_PREFIX = "live@"
LIVE_PROGRESS = "live_progress"
LIVE_BUTTON_ID = "live_progress"
_LIVE_TASK_ID = re.compile(r"live@([1-9][0-9]{0,18})")

# 工具名 → 人话动作；不认识的工具统一叫「调用工具」，状态行不露内部工具名。
_TOOL_ACTIONS = {
    "bash": "运行命令",
    "shell": "运行命令",
    "read": "读文件",
    "write": "改文件",
    "edit": "改文件",
    "multiedit": "改文件",
    "file_change": "改文件",
    "apply_patch": "改文件",
    "notebookedit": "改文件",
    "grep": "搜索",
    "glob": "搜索",
    "list": "搜索",
    "websearch": "查网页",
    "web_search": "查网页",
    "webfetch": "查网页",
    "task": "派子任务",
    "agent": "派子任务",
    "todowrite": "更新计划",
    "update_plan": "更新计划",
}


def tool_action(name: str) -> str:
    return _TOOL_ACTIONS.get(name.lower(), "调用工具")


@dataclass(frozen=True)
class LongWait:
    """一条已知的长等待：说明、通常要多久（秒）。"""

    what: str
    low: int
    high: int

    @property
    def expect(self) -> str:
        if self.low == self.high:
            return f"约 {_minutes(self.high)}"
        return f"通常 {self.low // 60}–{_minutes(self.high)}"


def _minutes(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} 秒"
    minutes = round(seconds / 60)
    return f"{minutes} 分钟"


_SLEEP = re.compile(r"\bsleep\s+([0-9]{1,6})(s|m|h)?\b")
_RULES: tuple[tuple[re.Pattern[str], LongWait], ...] = (
    (re.compile(r"\bgh\s+run\s+watch\b"), LongWait("等 GitHub Actions 跑完", 300, 600)),
    (re.compile(r"\bgh\s+pr\s+checks\b[^\n|;&]*--watch\b"), LongWait("等 PR 检查跑完", 300, 600)),
    (re.compile(r"\bkubectl\s+rollout\s+status\b"), LongWait("等部署滚动完成", 60, 300)),
)
# sleep 太短不算长等待：一分钟内本来就不会报静默。
_SLEEP_MIN_SECONDS = 60
_SLEEP_MAX_SECONDS = 6 * 3600


def long_wait(command: str) -> LongWait | None:
    """按关键词认出已知的长等待；认不出返回 None。多条命令连在一起时取最长的那个。"""
    if not command:
        return None
    found: list[LongWait] = [wait for pattern, wait in _RULES if pattern.search(command)]
    for match in _SLEEP.finditer(command):
        seconds = int(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2) or ""]
        if _SLEEP_MIN_SECONDS <= seconds <= _SLEEP_MAX_SECONDS:
            found.append(LongWait(f"等待 {_minutes(seconds)}", seconds, seconds))
    return max(found, key=lambda wait: wait.high, default=None)


def tool_detail(arguments: str) -> tuple[str, str]:
    """老节点没有 summary 时从工具参数里取：(说明, 命令)。Codex / OpenCode 的参数里本来就有。"""
    if not arguments:
        return "", ""
    try:
        parsed: Any = json.loads(arguments)
    except ValueError:
        return "", ""
    if not isinstance(parsed, dict):
        return "", ""
    command = parsed.get("command")
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    description = parsed.get("description")
    return (
        str(description).strip() if isinstance(description, str) else "",
        str(command).strip() if isinstance(command, str) else "",
    )


def first_line(text: str, limit: int) -> str:
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return clip(line, limit)


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat()


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def clock(at: datetime, now: datetime) -> str:
    """绝对时间：同一天只写 时:分，跨天带上月-日。"""
    local, today = at.astimezone(DISPLAY_TZ), now.astimezone(DISPLAY_TZ)
    if local.date() == today.date():
        return local.strftime("%H:%M")
    return local.strftime("%m-%d %H:%M")


def tool_activity(
    name: str, summary: str, command: str, now: datetime
) -> tuple[dict[str, Any], str]:
    """一次工具开始的 activity 与思考区那一行（不含 🔧）。

    说明优先用节点给的 summary，其次命令首行；都没有就只写动作（老节点）。
    """
    action = tool_action(name)
    wait = long_wait(command)
    # Codex 没有人写的说明，节点拿命令本身充当 summary：认得出的长等待用规则里的说法更好懂。
    if summary and wait and summary.rstrip("…") in command:
        summary = ""
    detail = summary or (wait.what if wait else "") or first_line(command, SUMMARY_CHARS)
    activity: dict[str, Any] = {
        "at": iso(now),
        "label": f"{action}：{detail}" if detail else action,
    }
    line = f"**{action}**：{detail}" if detail else f"**{name}**"
    if wait is not None:
        activity["until"] = iso(now + timedelta(seconds=wait.high))
        activity["expect"] = wait.expect
        line += f"（{wait.expect}）"
    return activity, line


def heading(running_since: datetime | None, activity: dict[str, Any], now: datetime) -> str:
    """运行中的状态行。"""
    at = parse_time(activity.get("at"))
    label = clip(str(activity.get("label") or ""), LABEL_CHARS)
    suffix = f"（{label}）" if label else ""
    if at is not None and activity.get("stuck"):
        return f"⚠️ 疑似卡住：{clock(at, now)} 后没有任何输出{suffix}"
    until = parse_time(activity.get("until"))
    waiting = until is not None and now < until
    if at is not None and not waiting and (now - at).total_seconds() >= QUIET_SECONDS:
        return f"⚠️ {clock(at, now)} 后没有新动作{suffix}"
    parts = ["⏳ 进行中"]
    if running_since is not None:
        parts.append(f"{clock(running_since, now)} 开始")
    if at is not None:
        parts.append(f"最后动作 {clock(at, now)}{suffix}")
    if waiting and until is not None:
        expect = str(activity.get("expect") or "")
        parts.append(f"{expect} · 预计 {clock(until, now)} 前" if expect else "")
    return " · ".join(part for part in parts if part)


def final_heading(activity: dict[str, Any]) -> str:
    reason = activity.get("interrupted")
    if isinstance(reason, str) and reason:
        return INTERRUPTED_PREFIX + reason
    return DONE_HEADING


def status_heading(
    *,
    running_since: datetime | None,
    activity: dict[str, Any] | None,
    complete: bool,
    now: datetime,
) -> str:
    activity = activity or {}
    return final_heading(activity) if complete else heading(running_since, activity, now)


def interrupt_reason(error_code: str | None, error_message: str | None) -> str | None:
    """worker 自己收尾时，哪些失败算「中断」（卡片标红、@ 提问人）。"""
    if error_code == "run_interrupted":
        return REASON_RELAY_INTERRUPTED
    if error_code == "hard_ttl":
        return REASON_HARD_TTL
    if error_code == "RelayError" and "秒无字节" in (error_message or ""):
        return REASON_RELAY_SILENT
    return None


def interrupted_activity(activity: dict[str, Any] | None, reason: str) -> dict[str, Any]:
    """收尸、恢复这类平台替任务收尾的路径：保留最后动作，记下中断原因。"""
    kept = {k: v for k, v in (activity or {}).items() if k not in ("until", "stuck")}
    return {**kept, "interrupted": reason}


def interrupt_notice(reason: str) -> str:
    return f"刚才这条处理中断了（{reason}）。已经写出的内容留在卡片里，可以再发一次让我接着做。"


def live_task_id(task_id: int) -> str:
    return f"{LIVE_PREFIX}{task_id}"


def parse_live_task_id(value: str) -> int | None:
    match = _LIVE_TASK_ID.fullmatch(value)
    return int(match.group(1)) if match else None


def live_button(task_id: int) -> dict[str, Any]:
    """运行中卡片最下面的「看看进度」：点了回一条实时进度，不改卡片。"""
    return {
        "tag": "button",
        "element_id": LIVE_BUTTON_ID,
        "text": {"tag": "plain_text", "content": "看看进度"},
        "type": "default",
        "size": "small",
        "behaviors": [
            {
                "type": "callback",
                "value": {"task_id": live_task_id(task_id), "event_key": LIVE_PROGRESS},
            }
        ],
    }

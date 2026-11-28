"""任务详情的纯文本（Markdown）版：给 AI 读，去掉页面样式和按钮，只留内容。

输入是任务详情接口返回的那份数据（见 view.detail）。内容全给、不截断，只去掉页面样式和按钮。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

STATUS_LABELS = {
    "shadow": "试运行",
    "todo": "待开始",
    "in_progress": "进行中",
    "waiting": "等待中",
    "review": "待验收",
    "done": "已完成",
    "cancelled": "已取消",
}


def _time(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M %Z").strip()
    return str(value or "")


def _actor(actor: dict[str, Any] | None) -> str:
    if not actor:
        return "—"
    return f"{actor['name']}（AI）" if actor.get("type") == "bot" else str(actor.get("name") or "—")


def _status(value: str) -> str:
    return STATUS_LABELS.get(value, value)


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _cell(text: str) -> str:
    return _one_line(text).replace("|", "\\|") or "—"


def render(d: dict[str, Any]) -> str:
    out: list[str] = [f"# {d['code']} {d['title']}", ""]
    meta = [
        ("状态", _status(d["status"])),
        ("执行人", _actor(d.get("assignee"))),
        ("发起人", (d.get("requester") or {}).get("name") or d.get("requester_name") or "—"),
        ("代表人", _actor(d.get("principal"))),
    ]
    if d.get("review_required"):
        meta.append(("验收人", _actor(d.get("reviewer") or d.get("principal"))))
    if d.get("dispatcher"):
        meta.append(("调度员", d["dispatcher"]["name"]))
    if d.get("priority") and d["priority"] != "normal":
        meta.append(("优先级", d["priority"]))
    if d.get("due_at"):
        meta.append(("截止", _time(d["due_at"])))
    meta.append(("创建", _time(d.get("created_at"))))
    if d.get("closed_at"):
        meta.append(("结束", _time(d["closed_at"])))
    if d.get("parent"):
        meta.append(("父任务", f"{d['parent']['code']} {d['parent']['title']}"))
    if d.get("related_codes"):
        meta.append(("相关任务", "、".join(d["related_codes"])))
    out += [f"- {k}：{v}" for k, v in meta]
    out += ["", f"现在：{_one_line(d.get('now') or '')}"]

    ask = d.get("ask")
    if ask:
        out += ["", "## 待回答", "", f"问{_actor(ask.get('to'))}：{ask['question']}"]
        if ask.get("why"):
            out.append(f"原因：{ask['why']}")
        out += [f"- {o['key']}：{o['label']}" for o in ask.get("options") or []]

    if d.get("summary"):
        out += ["", "## 一句话", "", d["summary"].strip()]
    if d.get("artifacts"):
        out += ["", "## 产出物", ""]
        out += [f"- [{a['title']}]({a['url']})" for a in d["artifacts"]]

    children = d.get("children") or []
    if children:
        out += ["", "## 子任务", "", "| 编号 | 任务 | 状态 | 执行人 | 进展 |"]
        out.append("|---|---|---|---|---|")
        out += [
            f"| {c['code']} | {_cell(c['title'])} | {_status(c['status'])} "
            f"| {_cell(_actor(c.get('assignee')))} | {_cell(c.get('now') or '')} |"
            for c in children
        ]

    agent = d.get("agent") or {}
    for key, title in (("report", "交付报告"), ("brief", "交给 AI 员工的背景"), ("digest", "记要")):
        if agent.get(key):
            out += ["", f"## {title}", "", agent[key].strip()]
    if agent.get("waiting_on"):
        out += ["", "## 在等", "", _json(agent["waiting_on"])]

    events = d.get("events") or []
    if events:
        out += ["", "## 经过", ""]
        for e in events:
            who = f"{e['actor_name']}：" if e.get("actor_name") else ""
            said = _one_line(e.get("human_text") or "")
            out.append(f"- {_time(e['created_at'])} [{e['kind']}] {who}{said}".rstrip())
            if e.get("agent_detail"):
                out.append(f"  - 细节：{_json(e['agent_detail'])}")

    runs = agent.get("runs") or []
    if runs:
        out += ["", "## AI 员工运行记录", ""]
        for r in runs:
            bot = (r.get("bot") or {}).get("name") or "—"
            line = f"- {_time(r['started_at'])} {bot} {r['trigger']} {r['status']}"
            if r.get("outcome"):
                line += f"：{_one_line(r['outcome'])}"
            out.append(line)

    decision = agent.get("decision")
    if decision:
        judged = f"判断：{decision['outcome']}，概率 {decision['probability']}"
        out += ["", "## 调度依据", "", judged]
        for m in decision.get("recent_messages") or []:
            out.append(f"- {m.get('sender', '')}：{_one_line(m.get('text', ''))}")
        if decision.get("dispatch"):
            out += ["", f"调度员原始输出：{_json(decision['dispatch'])}"]

    return "\n".join(out).rstrip() + "\n"

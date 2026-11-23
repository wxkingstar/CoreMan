"""Claude 的后台阶段会话（驱动的 /live/*）：放行哪些请求，以及各代驱动手上还有哪些长驻会话。

主回合结束时还有后台任务的会话，驱动不让进程退出，进入后台阶段（见驱动的 /live/list）。
守护进程据此：把这些会话算进占用的槽位，有这类会话的旧驱动不回收，心跳里上报会话标识。
"""

from __future__ import annotations

import asyncio
import re

CAPABILITY = "claude_background_sessions_v1"
# 只对 claude 放行；其余后端按「请求路径不支持」拒绝。attach 是长连接（驱动每 20 秒一行保活）。
PATHS = frozenset(
    {
        ("POST", "/live/attach"),
        ("POST", "/live/stop_task"),
        ("POST", "/live/close"),
        ("GET", "/live/list"),
    }
)
CLOSE_REASONS = frozenset({"user", "expired", "reset"})
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
# 会话表的缓存时长：心跳和轮询都读它，一轮最多问驱动一次。
LIST_TTL = 5.0
LIST_TIMEOUT = 5.0
# 某代驱动连续读失败这么多次后按没有后台会话处理，坏掉的旧驱动不会永远回收不掉。
FAILURE_LIMIT = 3


def validate(path: str, data: object) -> dict:
    """检查 /live/* 的请求体，返回原样转发的内容（只留驱动认的字段）。"""
    if path == "/live/list":
        return {}
    if not isinstance(data, dict):
        raise ValueError("请求体必须是对象")
    sid = data.get("session_id")
    if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
        raise ValueError("会话标识无效")
    body = {"session_id": sid}
    if path == "/live/stop_task":
        task = data.get("task_id")
        if not isinstance(task, str) or not TASK_ID.fullmatch(task):
            raise ValueError("后台任务标识无效")
        body["task_id"] = task
    elif path == "/live/close":
        if data.get("reason") not in CLOSE_REASONS:
            raise ValueError("结束原因无效")
        body["reason"] = data["reason"]
    return body


def parse_sessions(payload: object) -> list[dict]:
    """驱动 /live/list 的会话；形状不对的条目丢掉，会话标识不合规的也丢掉。

    驱动会把刚结束的会话留一阵（供 attach 补发），带 ``ended: true``：它们已不在后台阶段，
    不占槽位、不上报、不拖住旧驱动，这里一并丢掉。
    """
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    if not isinstance(sessions, list):
        raise ValueError("live list malformed")
    return [
        item
        for item in sessions
        if isinstance(item, dict)
        and isinstance(item.get("session_id"), str)
        and SESSION_ID.fullmatch(item["session_id"])
        and item.get("ended") is not True
    ]


class LiveTable:
    """claude 各代驱动的后台阶段会话（按驱动代次）。只在事件循环里改，同步代码只读。"""

    def __init__(self) -> None:
        self.sessions: dict[int, list[dict]] = {}
        # 每代最近一次读到确定结果的时刻（monotonic，取读之前的时刻）。
        self.fetched: dict[int, float] = {}
        # 每代最近一次经驱动的对话结束的时刻：在它之后读到会话表，才能确定这代驱动可以回收。
        self.call_ended: dict[int, float] = {}
        self.failures: dict[int, int] = {}
        self.checked = float("-inf")
        self.lock = asyncio.Lock()

    def ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for gen in sorted(self.sessions):
            for item in self.sessions[gen]:
                seen.setdefault(item["session_id"])
        return list(seen)

    def gen_of(self, session_id: str) -> int | None:
        for gen in sorted(self.sessions, reverse=True):
            if any(item["session_id"] == session_id for item in self.sessions[gen]):
                return gen
        return None

    def holds(self, gen: int) -> bool:
        """这代驱动不能回收：手上有后台会话，或最近一次对话结束后还没读到会话表。"""
        if self.sessions.get(gen):
            return True
        ended = self.call_ended.get(gen)
        return ended is not None and self.fetched.get(gen, float("-inf")) <= ended

    def forget(self, gen: int) -> None:
        for table in (self.sessions, self.fetched, self.call_ended, self.failures):
            table.pop(gen, None)

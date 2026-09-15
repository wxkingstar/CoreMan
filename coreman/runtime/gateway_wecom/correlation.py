"""推送关联表：把发出去的 req_id 记下来，等企微回错误码时能反查出这是哪一次推送。

企微的错误响应包只有 `errcode/errmsg/headers.req_id`，日志里孤零零
一个 6000/846608 根本没法定位是哪条流、哪个动作。发送侧每次推送都 `put` 一条，收到错误码时
`lookup` 把 `action/stream_id/extra/age` 一起打进 WARNING——这是线上排查这类问题的唯一线索。

进程内存结构，不落库：条目只在秒级到分钟级有用（企微的 stream 生命也就 10 分钟），重启后
丢掉也无所谓。为此有两道容量闸——TTL 过期与满时淘汰——免得网关跑上几天把内存撑爆。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

_PURGE_EVERY = 100
_EVICT_RATIO = 0.2


class PushCorrelation:
    """`req_id → 推送上下文` 的有界 TTL 字典。

    Attributes:
        ttl_seconds: 条目有效期，过期后 `lookup` 视同不存在
        max_entries: 容量上限，写满时淘汰最老的一批
    """

    def __init__(
        self,
        ttl_seconds: float = 300.0,
        max_entries: int = 5000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock
        # 依赖 dict 的插入序 = 写入时间序：淘汰最老的只要从头取键，不用排序。
        self._entries: dict[str, dict[str, Any]] = {}
        self._puts = 0

    def put(
        self,
        req_id: str,
        *,
        bot_key: str,
        action: str,
        stream_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self._puts += 1
        if self._puts % _PURGE_EVERY == 0:
            self._purge_expired()
        # 重复 req_id 先删后插，保证插入序仍然是「最近写入」序。
        self._entries.pop(req_id, None)
        if len(self._entries) >= self.max_entries:
            self._evict_oldest()
        self._entries[req_id] = {
            "bot_key": bot_key,
            "action": action,
            "stream_id": stream_id,
            "extra": extra,
            "ts": self._clock(),
        }

    def lookup(self, req_id: str) -> dict[str, Any] | None:
        """命中返回上下文副本（额外带 `age` 秒）；不存在或已过期返回 None。"""
        entry = self._entries.get(req_id)
        if entry is None:
            return None
        age = self._clock() - entry["ts"]
        if age > self.ttl_seconds:
            del self._entries[req_id]
            return None
        return {**entry, "age": age}

    def __len__(self) -> int:
        return len(self._entries)

    def _purge_expired(self) -> None:
        now = self._clock()
        for key in [k for k, v in self._entries.items() if now - v["ts"] > self.ttl_seconds]:
            del self._entries[key]

    def _evict_oldest(self) -> None:
        """写满时先清过期，还满就砍掉最老的 20%（一次腾出足够空间，别每次写都淘汰）。"""
        self._purge_expired()
        if len(self._entries) < self.max_entries:
            return
        drop = max(1, int(self.max_entries * _EVICT_RATIO))
        for key in list(self._entries)[:drop]:
            del self._entries[key]

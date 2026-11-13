"""个人工具报给调用方（模型、worker、轮询循环）的错误：只带代码与上游的数字错误码。

单独成模块：`ratelimit` 要继承它，`service` 要用 `ratelimit`，放在 `service` 里会成环。
`service.PersonalError` 仍是这个类。
"""

from __future__ import annotations

from typing import Any


class PersonalError(Exception):
    def __init__(self, code: str, *, upstream_code: int | None = None):
        self.code = code
        self.upstream_code = upstream_code
        super().__init__(code)

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": self.code}
        if self.upstream_code is not None:
            out["upstream_code"] = self.upstream_code
        return out

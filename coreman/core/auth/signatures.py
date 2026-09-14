"""旧 ApiSignature.php 兼容算法；不得记录待签名串（含客户端密钥）。"""

from __future__ import annotations

import hashlib
import math
from typing import Any
from urllib.parse import quote_plus


def _scalar(value: Any) -> str:
    # PHP (string) false/null 均为空，true 为 1。
    if value is None or value is False:
        return ""
    if value is True:
        return "1"
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite signature parameter")
    return str(value)


def canonical_params(params: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in sorted(params.items()):
        encoded = quote_plus(key, safe="").replace("~", "%7E")
        if isinstance(value, dict):
            # 保留旧协议：含嵌套对象的数据不参与签名；端点仍须校验权限与整个 body。
            continue
        if isinstance(value, list):
            if any(isinstance(item, (dict, list)) for item in value):
                continue
            for index, item in enumerate(value):
                parts.append(
                    f"{encoded}[{index}]=" + quote_plus(_scalar(item), safe="").replace("~", "%7E")
                )
        else:
            parts.append(f"{encoded}=" + quote_plus(_scalar(value), safe="").replace("~", "%7E"))
    return "&".join(parts)


def sign_request(
    method: str, path: str, params: dict[str, Any], timestamp: str, app_key: str, secret: str
) -> str:
    raw = (
        method.upper() + path.lstrip("/") + canonical_params(params) + timestamp + app_key + secret
    )
    return hashlib.sha256(raw.encode()).hexdigest()

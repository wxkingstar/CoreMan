"""凭证脱敏（spec §10.3：前2••••后2，提交含 •••• 视为未修改）。"""

from __future__ import annotations

MASK = "••••"


def mask_secret(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) <= 4:
        return MASK
    return f"{value[:2]}{MASK}{value[-2:]}"


def is_masked(value: str | None) -> bool:
    return value is not None and MASK in value

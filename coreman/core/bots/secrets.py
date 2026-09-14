"""bots 的凭证与环境变量：JSON 整体加密、脱敏、合并（本计划裁决 6）。"""

from __future__ import annotations

import json
import re

from coreman.core.crypto import Cipher
from coreman.core.masking import is_masked, mask_secret

CREDENTIALS_AAD = "bots.credentials_enc"
ENV_AAD = "bots.env_vars_enc"
ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
CREDENTIAL_KEYS: dict[str, tuple[tuple[str, bool], ...]] = {
    "wecom": (("bot_id", True), ("secret", True)),
    "feishu": (
        ("app_id", True),
        ("app_secret", True),
        ("encrypt_key", False),
        ("verification_token", False),
    ),
}


def encrypt_json(cipher: Cipher, data: dict[str, str], aad: str) -> str:
    return cipher.encrypt(json.dumps(data, ensure_ascii=False, sort_keys=True), aad)


def decrypt_json(cipher: Cipher, token: str, aad: str) -> dict[str, str]:
    if not token:
        return {}
    raw = json.loads(cipher.decrypt(token, aad))
    return {str(k): str(v) for k, v in raw.items()}


def mask_dict(data: dict[str, str]) -> dict[str, str]:
    return {k: mask_secret(v) or "" for k, v in data.items()}


def has_masked(data: dict[str, str]) -> bool:
    """任一值含 •••• —— 创建时没有可回退的旧值，只能拒。"""
    return any(is_masked(v) for v in data.values())


def merge_secret_dict(current: dict[str, str], incoming: dict[str, str]) -> dict[str, str]:
    """脱敏值 = 该键不变；键缺失 = 删除。"""
    merged: dict[str, str] = {}
    for key, value in incoming.items():
        if is_masked(value):
            if key not in current:
                raise ValueError(f"键 {key} 的脱敏值没有可回退的旧值")
            merged[key] = current[key]
        else:
            merged[key] = value
    return merged


def validate_credentials(platform: str, creds: dict[str, str]) -> None:
    spec = CREDENTIAL_KEYS[platform]
    known = {k for k, _ in spec}
    missing = [k for k, required in spec if required and not creds.get(k)]
    unknown = sorted(set(creds) - known)
    if missing:
        raise ValueError(f"缺少凭证字段：{', '.join(missing)}")
    if unknown:
        raise ValueError(f"未知凭证字段：{', '.join(unknown)}")

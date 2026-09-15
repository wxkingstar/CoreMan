"""运行时节点管理端与节点协议端共用的校验与令牌摘要。"""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath


def token_digest(token: str) -> str:
    """安装链接与节点凭证只以 SHA-256 摘要落库。"""
    return hashlib.sha256(token.encode()).hexdigest()


def absolute_root(value: str) -> str:
    """项目主目录：无上级跳转、无控制字符的绝对路径，且不能是系统根目录。"""
    if (
        not value.startswith("/")
        or value.startswith("//")
        or ".." in value.split("/")
        or any(ord(c) < 32 for c in value)
        or "\x7f" in value
    ):
        raise ValueError("项目主目录必须是无上级跳转的绝对路径")
    result = str(PurePosixPath(value))
    if result == "/":
        raise ValueError("不能以系统根目录作为项目主目录")
    return result

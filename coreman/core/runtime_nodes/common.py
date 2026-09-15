"""运行时节点管理端与节点协议端共用的校验与令牌摘要。"""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath

# 节点协议版本。2：领取命令支持长轮询、响应帧可批量回传、心跳上报节点并发。
# 节点在注册与心跳里上报自己的版本，管理端在响应里返回自己的版本，双方都按低者行事。
PROTOCOL_VERSION = 2


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

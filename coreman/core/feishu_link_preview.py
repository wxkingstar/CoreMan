"""飞书链接预览：管理台的会话查看链接、任务链接在飞书消息里展开成一行说明。

飞书的规则：一个企业里同一个网址规则只能由一个应用注册，所以由部署指定的那一个飞书应用（通常是
飞书登录用的平台应用）在开发者后台添加「链接预览」能力、注册管理台域名、把回调地址配成
`/api/integrations/feishu/link-preview` 并发布。回调只走 HTTP（不走长连接），3 秒内要回。

预览是千人千面的：飞书按查看的人分别来拉，这里按查看的人能看到什么决定写什么——
- 会话查看链接：本人的私聊会话，或管理员可看的群聊会话，写「员工名 · 会话记录 · N 轮」；
  其余（看不了的、飞书私聊别人的、个人助理）只写「CoreMan 会话记录」。
- 任务链接：在职成员都能看任务页，写「任务 编号 · 标题 · 状态」；认不出的人只写编号。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.permissions import MANAGER_ROLES
from coreman.core.db.models import Bot, ChatLog, User, UserIdentity, WorkTask

PATH = "/api/integrations/feishu/link-preview"
EVENT_TYPE = "url.preview.get"
TITLE_CHARS = 60
_SESSION = re.compile(
    r"/api/admin/runtime-nodes/[0-9a-fA-F-]{36}/[a-z]+/session/([0-9a-fA-F-]{36})/?\Z"
)
_TASK = re.compile(r"/work-tasks/([A-Za-z0-9_-]{1,40})/?\Z")
_STATUS = {
    "shadow": "试运行",
    "todo": "待开始",
    "in_progress": "进行中",
    "waiting": "等待中",
    "review": "待验收",
    "done": "已完成",
    "cancelled": "已取消",
}


class PreviewError(Exception):
    """回调验不过（令牌、应用不对，或解密失败）。"""


def decrypt(encrypt_key: str, encrypted: str) -> dict[str, Any]:
    """飞书事件加密：AES-256-CBC，密钥是 Encrypt Key 的 SHA-256，前 16 字节是 IV。"""
    try:
        raw = base64.b64decode(encrypted)
        key = hashlib.sha256(encrypt_key.encode()).digest()
        decryptor = Cipher(algorithms.AES(key), modes.CBC(raw[:16])).decryptor()
        padded = decryptor.update(raw[16:]) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        body = json.loads((unpadder.update(padded) + unpadder.finalize()).decode())
    except (ValueError, TypeError) as exc:
        raise PreviewError("decrypt") from exc
    if not isinstance(body, dict):
        raise PreviewError("decrypt")
    return body


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= TITLE_CHARS else text[: TITLE_CHARS - 1] + "…"


async def _viewer(session: AsyncSession, user_id: str) -> User | None:
    if not user_id:
        return None
    user: User | None = await session.scalar(
        select(User)
        .join(UserIdentity, UserIdentity.user_id == User.id)
        .where(
            UserIdentity.platform == "feishu",
            UserIdentity.platform_user_id == user_id,
            User.status == "active",
        )
    )
    return user


async def _session_title(session: AsyncSession, viewer: User | None, sid: uuid.UUID) -> str:
    generic = "CoreMan 会话记录"
    rows = (
        await session.execute(
            select(
                ChatLog.platform,
                ChatLog.chat_type,
                ChatLog.user_id,
                ChatLog.private,
                ChatLog.bot_id,
            )
            .where(ChatLog.relay_session_id == sid)
            .limit(500)
        )
    ).all()
    if viewer is None or not rows:
        return generic
    bots = {row.bot_id for row in rows}
    if await session.scalar(
        select(Bot.id).where(Bot.id.in_(bots), Bot.kind == "assistant").limit(1)
    ):
        return generic
    own = all(
        (row.chat_type == "single" or row.private) and row.user_id == viewer.id for row in rows
    )
    manager = viewer.role in MANAGER_ROLES and not any(
        (row.platform == "feishu" and row.chat_type == "single") or row.private for row in rows
    )
    if not (own or manager):
        return generic
    names = list(await session.scalars(select(Bot.name).where(Bot.id.in_(bots)).order_by(Bot.name)))
    return _clip(f"{'、'.join(names) or 'AI 员工'} · 会话记录 · {len(rows)} 轮")


async def _task_title(session: AsyncSession, viewer: User | None, code: str) -> str:
    task = await session.scalar(select(WorkTask).where(func.lower(WorkTask.code) == code.lower()))
    if task is None:
        return f"CoreMan 任务 {code}"
    if viewer is None:
        return f"CoreMan 任务 {task.code}"
    status = _STATUS.get(task.status, task.status)
    return _clip(f"任务 {task.code} · {task.title} · {status}")


async def preview(
    session: AsyncSession, *, base_url: str, url: str, operator_user_id: str
) -> dict[str, Any]:
    """回调的响应体；不是管理台的会话、任务链接时返回空对象（飞书照原样显示链接）。"""
    target, base = urlsplit(url), urlsplit(base_url)
    if not base.netloc or target.netloc.lower() != base.netloc.lower():
        return {}
    viewer = await _viewer(session, operator_user_id)
    if match := _SESSION.search(target.path):
        title = await _session_title(session, viewer, uuid.UUID(match.group(1)))
    elif match := _TASK.search(target.path):
        title = await _task_title(session, viewer, match.group(1))
    else:
        return {}
    return {"inline": {"title": title}}

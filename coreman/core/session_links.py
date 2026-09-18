"""私聊会话查看链接的凭据：绑定本人、会话、节点与过期时间，飞书资料模式再绑上下文版本。

凭据只是登录之外的第二道门：打开时仍要登录，且登录的就是链接签给的那个人。
AES-GCM 同时保证不可伪造与不外露（用户 ID、上下文版本不以明文出现在 URL 里）。
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import dataclass

from coreman.core.crypto import PREFIX, Cipher, DecryptError

AAD = "session_viewer.link.v1"
TTL_SECONDS = 24 * 3600


@dataclass(frozen=True)
class LinkClaims:
    session_id: uuid.UUID
    user_id: uuid.UUID
    node_id: uuid.UUID
    provider: str
    bot_id: uuid.UUID
    expires_at: float
    # 飞书资料模式的链接才有：撤销授权、切换模式、重新授权都会换掉它，旧链接随之作废。
    context_epoch: uuid.UUID | None


def issue(
    cipher: Cipher,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    node_id: uuid.UUID,
    provider: str,
    bot_id: uuid.UUID,
    context_epoch: uuid.UUID | None = None,
    now: float | None = None,
) -> str:
    claims: dict[str, object] = {
        "s": str(session_id),
        "u": str(user_id),
        "n": str(node_id),
        "p": provider,
        "b": str(bot_id),
        "x": int((time.time() if now is None else now) + TTL_SECONDS),
    }
    if context_epoch is not None:
        claims["e"] = str(context_epoch)
    sealed = cipher.encrypt(json.dumps(claims, separators=(",", ":")), AAD)
    raw = base64.b64decode(sealed[len(PREFIX) :])
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def read(cipher: Cipher, token: str, *, now: float | None = None) -> LinkClaims | None:
    """解不开、过期或格式不对都返回 None；调用方不区分原因，免得给试探者提示。"""
    if not token or len(token) > 1024:
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        data = json.loads(cipher.decrypt(PREFIX + base64.b64encode(raw).decode("ascii"), AAD))
        claims = LinkClaims(
            session_id=uuid.UUID(data["s"]),
            user_id=uuid.UUID(data["u"]),
            node_id=uuid.UUID(data["n"]),
            provider=str(data["p"]),
            bot_id=uuid.UUID(data["b"]),
            expires_at=float(data["x"]),
            context_epoch=uuid.UUID(data["e"]) if data.get("e") else None,
        )
    except (DecryptError, ValueError, KeyError, TypeError):
        return None
    if claims.expires_at <= (time.time() if now is None else now):
        return None
    return claims

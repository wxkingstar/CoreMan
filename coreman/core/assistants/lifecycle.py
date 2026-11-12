"""开通与停用的公共件：一人一条进行中的流程、步骤的成败、给本人发消息。

流程记在 `assistant_lifecycles`，由看护进程（`lifecycle_runner.tick`）一步一步推进；开通的各步在
`provisioning`，停用的各步在 `deprovisioning`。每一步在自己的事务里做完并提交；失败就停在原地，
`next_attempt_at` 到了再试。

给本人发的消息一律经助理应用本身（`platform_apps` 的凭证），不经某条助理记录的出站箱：开通前还
没有记录，停用后记录已停用、出站循环不再发它的消息。消息里不带节点、主机或任何内部信息。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants.apps import app_credentials
from coreman.core.crypto import Cipher, DecryptError
from coreman.core.db.models import AssistantLifecycle, PlatformApp
from coreman.core.platforms.feishu import FeishuClient, FeishuError
from coreman.core.timeutils import utcnow

# 本步骤连续失败到这么多次：开通放弃（走一遍停用把已建的东西收掉）；停用永不放弃。
PROVISION_MAX_ATTEMPTS = 10
BACKOFF_MAX = timedelta(minutes=10)


class StepRetry(Exception):
    """这一步暂时做不成（节点离线、飞书或审批系统不通）：原地等下次。`code` 只是代码。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class StepFailed(Exception):
    """这一步再试也不会成（不在开通范围、应用权限不足……）：开通就此放弃。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def live(
    session: AsyncSession, owner_id: uuid.UUID, *, lock: bool = True, skip_locked: bool = False
) -> AssistantLifecycle | None:
    """这个人进行中的那一条流程（最多一条，见 `assistant_lifecycles_live_uq`）。

    `skip_locked`：别人正锁着就当没找到（不等锁）；调用方要自己分清「没有」与「被锁住」。
    """
    stmt = select(AssistantLifecycle).where(
        AssistantLifecycle.owner_id == owner_id, AssistantLifecycle.finished_at.is_(None)
    )
    if lock or skip_locked:
        stmt = stmt.with_for_update(skip_locked=skip_locked)
    found: AssistantLifecycle | None = await session.scalar(stmt)
    return found


def advance(row: AssistantLifecycle, step: str, *, at: datetime | None = None) -> None:
    """进入下一步：失败计数清零，`at`（默认现在）就接着做。"""
    row.step, row.attempts, row.error_code = step, 0, None
    row.next_attempt_at = at or utcnow()
    row.updated_at = utcnow()


def finish(row: AssistantLifecycle, outcome: str, code: str | None = None) -> None:
    now = utcnow()
    row.finished_at, row.outcome, row.next_attempt_at, row.updated_at = now, outcome, None, now
    row.offer_hash = None
    if code is not None:
        row.error_code = code


def backoff(attempts: int) -> timedelta:
    return min(BACKOFF_MAX, timedelta(seconds=15 * 2 ** min(attempts, 10)))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Messenger:
    """经助理应用给本人发消息与卡片；同一轮推进里按应用复用飞书客户端，用完 `aclose`。"""

    def __init__(self, cipher: Cipher) -> None:
        self.cipher = cipher
        self._clients: dict[uuid.UUID, FeishuClient] = {}

    async def _client(self, session: AsyncSession, app_id: uuid.UUID) -> FeishuClient:
        client = self._clients.get(app_id)
        if client is None:
            app = await session.get(PlatformApp, app_id)
            if app is None:
                raise StepFailed("app_missing")
            try:
                creds = app_credentials(self.cipher, app)
            except DecryptError:
                # 读不出应用密钥多半是主密钥轮换没做完：等一等再试，不能因此放弃开通。
                raise StepRetry("app_secret_unreadable") from None
            except ValueError:
                raise StepFailed("app_unavailable") from None
            client = FeishuClient(creds["app_id"], creds["app_secret"])
            self._clients[app_id] = client
        return client

    async def _send(
        self,
        session: AsyncSession,
        app_id: uuid.UUID,
        user_id: str,
        msg_type: str,
        content: dict[str, Any],
        key: str,
    ) -> tuple[str, str]:
        client = await self._client(session, app_id)
        try:
            body = await client.call(
                "POST",
                "/open-apis/im/v1/messages",
                params={"receive_id_type": "user_id"},
                json={
                    "receive_id": user_id,
                    "msg_type": msg_type,
                    "content": json.dumps(content, ensure_ascii=False),
                    "uuid": hashlib.sha256(key.encode()).hexdigest()[:32],
                },
            )
        except FeishuError as exc:
            raise StepRetry(f"feishu_{exc.code}") from None
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise StepRetry("feishu_bad_response")
        message_id, chat_id = data.get("message_id"), data.get("chat_id")
        if not isinstance(message_id, str) or not isinstance(chat_id, str):
            raise StepRetry("feishu_bad_response")
        return message_id, chat_id

    async def text(
        self, session: AsyncSession, app_id: uuid.UUID, user_id: str, text: str, key: str
    ) -> None:
        await self._send(session, app_id, user_id, "text", {"text": text}, key)

    async def card(
        self,
        session: AsyncSession,
        app_id: uuid.UUID,
        user_id: str,
        card: dict[str, Any],
        key: str,
    ) -> tuple[str, str]:
        return await self._send(session, app_id, user_id, "interactive", card, key)

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

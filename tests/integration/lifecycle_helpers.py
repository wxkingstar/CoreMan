"""开通与停用用例的公共件：成员、助理应用与助理池节点，以及飞书（消息、授权）与节点的替身。"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.assistants import keys, lifecycle, lifecycle_runner, retired_dirs, sealed
from coreman.core.assistants.apps import SECRET_AAD
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    AssistantLifecycle,
    Bot,
    FeishuPersonalGrant,
    PlatformApp,
    RelayServer,
    RuntimeNode,
    Task,
    User,
    UserIdentity,
)
from coreman.core.feishu_personal import revocation
from coreman.core.feishu_personal import service as personal
from coreman.core.relay.agent_client import AgentError
from coreman.core.timeutils import utcnow
from tests.fakes.runtime_node import add_node_relay
from tests.integration.assistant_helpers import MASTER_KEY

CIPHER = Cipher(MASTER_KEY)
OWNER_PID, OWNER_OPEN_ID, TENANT = "zhangsan", "ou_zhang", "tenant-example"
APP_ID = "cli_example"
CHAT = "oc_private_zhang"


class FakeMessenger(lifecycle.Messenger):
    """记下发出去的文本与卡片；`fail` 置位时发送失败（StepRetry）。"""

    def __init__(self) -> None:
        super().__init__(CIPHER)
        self.texts: list[tuple[str, str]] = []
        self.cards: list[tuple[str, dict[str, Any]]] = []
        self.fail = False

    async def _send(
        self,
        session: AsyncSession,
        app_id: uuid.UUID,
        user_id: str,
        msg_type: str,
        content: dict[str, Any],
        key: str,
    ) -> tuple[str, str]:
        if self.fail:
            raise lifecycle.StepRetry("feishu_99991400")
        if msg_type == "text":
            self.texts.append((user_id, content["text"]))
        else:
            self.cards.append((user_id, content))
        return f"om_{len(self.texts) + len(self.cards)}", CHAT

    def token(self) -> str:
        """最近一张开通卡片上的令牌。"""
        for _, card in reversed(self.cards):
            for element in card["body"]["elements"]:
                if element.get("tag") == "button":
                    value = element["behaviors"][0].get("value") or {}
                    if value.get("assistant") == "provision":
                        return str(value["token"])
        raise AssertionError("no offer card")

    def message_id(self) -> str:
        return f"om_{len(self.texts) + len(self.cards)}"


class FakeNode:
    """替换对节点的调用（建目录、删目录）；`down` 置位时节点不可达。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.down = False
        self.removed: dict[str, Any] = {"removed": True, "existed": True}

    async def __call__(
        self, relay: RelayServer, cipher: Any, operation: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if self.down:
            raise AgentError("Agent 连接失败（ConnectError）")
        self.calls.append((operation, dict(payload or {})))
        if operation == "workspace-remove":
            return {"success": True, **self.removed}
        return {"success": True, "initialized": True, "owned": True}

    def ops(self) -> list[str]:
        return [operation for operation, _ in self.calls]


async def pool_relay(session: AsyncSession, *, provider: str = "claude") -> RelayServer:
    """助理池里一个能跑助理的实例：节点在线、声明沙箱能力、上报指纹与非 root 用户。"""
    relay = await add_node_relay(
        session, name=f"pool-{uuid.uuid4().hex[:6]}", provider=provider, workspace_root="/srv/pa"
    )
    relay.assistant_pool = True
    node = await session.get(RuntimeNode, relay.runtime_node_id)
    assert node is not None
    node.capabilities = {
        provider: {"installed": True, "login": "ready", "assistant_sandbox_v1": True}
    }
    node.heartbeat_at = utcnow()
    node.machine_fingerprint, node.uid = f"fp-{node.id.hex}", 1001
    await session.flush()
    return relay


async def member(
    session: AsyncSession, *, login: str = "zhangsan", pid: str = OWNER_PID, open_id: str = ""
) -> User:
    user = User(login_name=login, display_name="张三", source="sync")
    session.add(user)
    await session.flush()
    session.add(
        UserIdentity(
            user_id=user.id, platform="feishu", platform_user_id=pid, open_id=open_id or None
        )
    )
    await session.flush()
    return user


async def assistant_app(session: AsyncSession) -> PlatformApp:
    app = PlatformApp(
        platform="feishu",
        name=f"个人助理-{uuid.uuid4().hex[:6]}",
        capabilities=["assistant"],
        app_id=APP_ID,
        secret_enc=CIPHER.encrypt("app-secret", SECRET_AAD),
    )
    session.add(app)
    await session.flush()
    return app


def message_event(pid: str = OWNER_PID) -> dict[str, Any]:
    return {
        "header": {
            "event_id": f"ev-{uuid.uuid4().hex}",
            "event_type": "im.message.receive_v1",
            "app_id": APP_ID,
            "tenant_key": TENANT,
        },
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"user_id": pid, "open_id": "ou_x"}},
            "message": {
                "message_id": f"om_in_{uuid.uuid4().hex[:8]}",
                "chat_id": CHAT,
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": "你好"}),
            },
        },
    }


def click_event(
    token: str,
    message_id: str,
    *,
    pid: str = OWNER_PID,
    open_id: str = OWNER_OPEN_ID,
    chat_id: str = CHAT,
) -> dict[str, Any]:
    return {
        "header": {
            "event_id": f"ev-{uuid.uuid4().hex}",
            "event_type": "card.action.trigger",
            "app_id": APP_ID,
            "tenant_key": TENANT,
        },
        "event": {
            "operator": {"user_id": pid, "open_id": open_id},
            "action": {"value": {"assistant": "provision", "token": token}},
            "context": {"open_message_id": message_id, "open_chat_id": chat_id},
        },
    }


async def flow_of(session: AsyncSession, owner_id: uuid.UUID) -> AssistantLifecycle | None:
    """这个人最近的一条流程（含已结束的），重新从库里读。"""
    found: AssistantLifecycle | None = await session.scalar(
        select(AssistantLifecycle)
        .where(AssistantLifecycle.owner_id == owner_id)
        .order_by(AssistantLifecycle.created_at.desc(), AssistantLifecycle.id)
        .limit(1)
        .execution_options(populate_existing=True)
    )
    return found


class FakeFeishuAuth:
    """飞书设备授权与撤销的替身：`approve()` 之后，下一次查询拿到本人的令牌。"""

    def __init__(self) -> None:
        self.approved = False
        self.issued = 0
        self.revoked: list[dict[str, Any]] = []
        self.identity = {"user_id": OWNER_PID, "open_id": OWNER_OPEN_ID, "tenant_key": TENANT}

    def approve(self) -> None:
        self.approved = True

    async def http(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        if url.endswith("/oauth/v1/device_authorization"):
            self.issued += 1
            return {
                "device_code": f"dc-{self.issued}",
                "verification_uri_complete": f"https://accounts.feishu.cn/verify?c={self.issued}",
                "expires_in": 600,
                "interval": 5,
            }
        if url == personal.TOKEN_URL:
            if not self.approved:
                return {"error": "authorization_pending"}
            return {
                "access_token": "u-access",
                "refresh_token": "u-refresh",
                "expires_in": 7200,
                "scope": "offline_access im:message",
            }
        if url.endswith("/authen/v1/user_info"):
            return {"code": 0, "data": self.identity}
        raise AssertionError(url)

    async def revoke(self, app_id: str, secret: str, tokens: dict[str, Any]) -> bool:
        self.revoked.append(tokens)
        return True


def install(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeNode, FakeFeishuAuth]:
    node, auth = FakeNode(), FakeFeishuAuth()
    monkeypatch.setattr(retired_dirs, "call_agent", node)
    monkeypatch.setattr(personal, "_http", auth.http)
    monkeypatch.setattr(revocation, "revoke_tokens", auth.revoke)

    async def no_scopes(*_args: Any, **_kw: Any) -> list[str]:
        # 应用开通了发消息与读消息的全部用户权限（「全部」档位要它们）。
        return ["offline_access", "im:message", "im:message.send_as_user", "im:chat:read"]

    from coreman.core.feishu_personal import permissions

    monkeypatch.setattr(permissions, "app_user_scopes", no_scopes)
    return node, auth


async def tick(
    factory: async_sessionmaker[AsyncSession], messenger: FakeMessenger, *, later: float = 0
) -> dict[str, int]:
    """推进一轮；`later` 秒之后的时刻（跳过退避与轮询间隔）。"""
    return await lifecycle_runner.tick(
        factory, CIPHER, utcnow() + timedelta(seconds=later), messenger=messenger
    )


async def active_assistant(session: AsyncSession) -> tuple[Bot, User, Task]:
    """一位已经开通好的助理（与所有者聊过一轮，那一轮已结束），带着有效的飞书授权。

    记录绑在助理池里一个能跑助理的实例上（`into_assistant` → `sandbox_ready`）。
    """
    from tests.integration.assistant_helpers import into_assistant
    from tests.integration.credential_helpers import owner

    bot, user, task, _ = await owner(session)
    await into_assistant(session, bot, user)
    await keys.ensure_key(session, sealed.master(), user.id)
    from coreman.core.bots.secrets import CREDENTIALS_AAD, encrypt_json

    # 助理记录上是助理应用凭证的副本（见 assistants.apps）。
    bot.credentials_enc = encrypt_json(
        CIPHER, {"app_id": APP_ID, "app_secret": "app-secret"}, CREDENTIALS_AAD
    )
    grant = FeishuPersonalGrant(
        bot_id=bot.id,
        user_id=user.id,
        app_id=APP_ID,
        platform_user_id="owner_pid",
        open_id="ou_owner",
        tenant_key=TENANT,
        status="connected",
        authorization_level="all",
        scopes=["offline_access"],
        requested_scopes=["offline_access"],
    )
    grant.token_enc = CIPHER.encrypt(
        json.dumps({"access_token": "u-access", "refresh_token": "u-refresh"}),
        personal._aad(grant, "token_enc"),
    )
    session.add(grant)
    task.status = "succeeded"
    await session.commit()
    return bot, user, task

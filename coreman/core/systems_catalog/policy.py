"""Task-bound capability for the catalog MCP, and the checks every call repeats.

The capability names a task and the verified speaker it was issued for. Nothing the runtime sends
can widen it: each call reloads the task, the AI employee and the member, and recomputes the
systems from the same grants that decide which business tokens a turn receives.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import kinds
from coreman.core.assistants.kinds import BOT_HELP_KIND
from coreman.core.bus import tasks
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, RelayServer, RuntimeNode, Task, User
from coreman.core.relay.models import backend_of

AAD = "systems_catalog.capability.v1"
ENV_PREFIX = "COREMAN_SYSTEMS_MCP_"
URL_ENV = ENV_PREFIX + "URL"
TOKEN_ENV = ENV_PREFIX + "TOKEN"
API_PATH = "/api/runtime/systems/mcp"
# A runtime declares this when its drivers mount `coreman_systems`; older ones keep the prompt
# without catalog tools.
RUNTIME_CAPABILITY = "systems_catalog_v1"


@dataclass(frozen=True)
class Mount:
    """Where this turn's catalog MCP lives. Only built for runtimes that can mount it."""

    task_id: int
    url: str
    ttl_seconds: int


@dataclass(frozen=True)
class Capability:
    task_id: int
    actor: uuid.UUID


@dataclass(frozen=True)
class Scope:
    task: Task
    bot: Bot
    user: User


def issue_capability(cipher: Cipher, *, task_id: int, user_id: uuid.UUID, ttl_seconds: int) -> str:
    claims = {"task": task_id, "actor": str(user_id), "exp": time.time() + ttl_seconds}
    return cipher.encrypt(json.dumps(claims), AAD)


def read_capability(cipher: Cipher, token: str) -> Capability:
    data = json.loads(cipher.decrypt(token, AAD))
    if not isinstance(data, dict) or data["exp"] <= time.time():
        raise ValueError("expired_capability")
    return Capability(int(data["task"]), uuid.UUID(str(data["actor"])))


def runtime_supported(capabilities: dict[str, Any] | None, provider: str) -> bool:
    capability = (capabilities or {}).get(provider) or {}
    return isinstance(capability, dict) and capability.get(RUNTIME_CAPABILITY) is True


async def mount_for(
    session: AsyncSession,
    *,
    relay: RelayServer,
    model: str,
    task_id: int,
    public_base_url: str,
    ttl_seconds: int,
) -> Mount | None:
    if relay.runtime_node_id is None:
        return None
    node = await session.get(RuntimeNode, relay.runtime_node_id, populate_existing=True)
    if node is None or not node.is_active:
        return None
    if not runtime_supported(node.capabilities, backend_of(model, relay.model_provider)):
        return None
    return Mount(task_id, public_base_url.rstrip("/") + API_PATH, ttl_seconds)


async def task_scope(session: AsyncSession, capability: Capability) -> Scope:
    task = await session.get(Task, capability.task_id, populate_existing=True)
    if task is None or task.status not in tasks.ACTIVE or task.cancel_requested_at:
        raise ValueError("task_not_active")
    bot = await session.get(Bot, task.bot_id, populate_existing=True)
    user = await session.get(User, capability.actor, populate_existing=True)
    if bot is None or not bot.enabled:
        raise ValueError("bot_unavailable")
    if user is None or user.status != "active" or user.source == "bootstrap":
        raise ValueError("member_unavailable")
    return Scope(task, bot, user)


def writable_for(task: Task, granted: Iterable[str]) -> frozenset[str]:
    """这一轮经目录代理能写哪些系统。

    个人助理调度来的一轮，所有者没有在审批系统里批准写（载荷里的 `allow_write` 不是真）时一个
    都不能写：这是平台自己把得住的写通道。个人助理无人在场的一轮（`kinds.unattended`）同样一个
    都不能直接写：所有者不在场，授权允许写的系统改为提交所有者审批（`proxy.OWNER_APPROVAL`，
    由路由按 `approvals.proxied.may_stage` 判断），批准后由平台另起执行。其余轮次照员工的授权。
    """
    if kinds.unattended(task.kind):
        return frozenset()
    if task.kind == BOT_HELP_KIND and task.payload.get("allow_write") is not True:
        return frozenset()
    return frozenset(granted)


def writes_preapproved(task: Task) -> bool:
    """所有者已在审批系统里批准这一轮改东西（个人助理调度来的一轮）：写操作不再发确认卡，
    这一轮也没有人在飞书里点卡。"""
    return task.kind == BOT_HELP_KIND and task.payload.get("allow_write") is True

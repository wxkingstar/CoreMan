"""个人助理用哪台运行时：只派到能在沙箱里跑的节点；池里挑助理最少的节点；节点不可用就暂停。

- 能派的节点：在线、启用、没在排空，且这种 AI 的能力里声明了 `assistant_sandbox_v1`（守护进程
  启动自检通过，见 runtime_daemon/sandbox.py）。macOS、容器里没有用户命名空间、没装 bubblewrap、
  还没升级的节点都不会声明它。
- 能绑的实例：助理池里的（`relay_servers.assistant_pool`），或所有者本人的私有运行时。
- 开通（1F）调 `pick_pool_relay` 选池里助理最少的在线节点；之后每轮都不重新选（设计「不做」）。
  所在节点不可用时这一轮不跑，私聊告诉所有者，不回落到池。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.relay_policy import relay_available
from coreman.core.db.models import Bot, RelayServer, RuntimeNode
from coreman.core.runtime_nodes.transport import online

SANDBOX_CAPABILITY = "assistant_sandbox_v1"
# 管理台设置项：每个节点同时跑的个人助理轮次上限（节点领取时执行）。
ASSISTANT_CONCURRENCY_KEY = "assistant_node_concurrency"
PAUSE_MESSAGES = {
    "runtime_missing": "还没有分配可用的运行时，请联系管理员",
    "runtime_not_allowed": "绑定的运行时既不在助理池里、也不是你本人的私有运行时，请联系管理员",
    "runtime_disabled": "绑定的运行时已停用，请联系管理员",
    "runtime_offline": "绑定的运行时现在离线，恢复在线后再发一次消息即可",
    "runtime_draining": "绑定的运行时正在维护，稍后再发一次消息即可",
    "sandbox_unsupported": "绑定的运行时还不能隔离运行个人助理（需要升级或不支持），请联系管理员",
    "login_required": "绑定的运行时 AI 未登录，请联系管理员",
}


def node_sandboxed(node: RuntimeNode, provider: str | None = None) -> bool:
    """节点（某种 AI，或任一种）声明了能在沙箱里跑助理。"""
    capabilities: dict[str, Any] = node.capabilities or {}
    providers = [provider] if provider else list(capabilities)
    return any(
        isinstance(capabilities.get(p), dict) and capabilities[p].get(SANDBOX_CAPABILITY) is True
        for p in providers
    )


def unavailable_reason(relay: RelayServer, node: RuntimeNode | None) -> str | None:
    """这个实例现在能不能跑助理的一轮；能跑返回 None。"""
    if not relay_available(relay) or node is None or not node.is_active:
        return "runtime_disabled"
    if not online(node):
        return "runtime_offline"
    if node.draining:
        return "runtime_draining"
    if not node_sandboxed(node, relay.model_provider):
        return "sandbox_unsupported"
    cap = (node.capabilities or {}).get(relay.model_provider) or {}
    if cap.get("login") != "ready":
        return "login_required"
    return None


def eligible(relay: RelayServer, owner_id: uuid.UUID | None) -> bool:
    """助理能不能绑这个实例：助理池里的，或所有者本人的私有运行时。"""
    if relay.assistant_pool:
        return True
    return relay.visibility == "private" and owner_id is not None and relay.owner_id == owner_id


async def turn_blocker(session: AsyncSession, bot: Bot) -> str | None:
    """助理现在为什么不能跑一轮；能跑返回 None。"""
    relay = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
    if relay is None:
        return "runtime_missing"
    if not eligible(relay, bot.owner_id):
        return "runtime_not_allowed"
    node = await session.get(RuntimeNode, relay.runtime_node_id) if relay.runtime_node_id else None
    return unavailable_reason(relay, node)


def pause_message(reason: str) -> str:
    detail = PAUSE_MESSAGES.get(reason, PAUSE_MESSAGES["runtime_missing"])
    return f"你的个人助理暂停了：{detail}。"


def assignment_error(
    relay: RelayServer, node: RuntimeNode | None, owner_id: uuid.UUID | None
) -> str | None:
    """改绑时的校验：只能绑助理池或本人的私有运行时，且那台现在就能跑助理。"""
    if not eligible(relay, owner_id):
        return "个人助理只能使用助理池或你本人的私有运行时"
    reason = unavailable_reason(relay, node)
    return None if reason is None else PAUSE_MESSAGES[reason].removeprefix("绑定的")


async def pick_pool_relay(session: AsyncSession, provider: str) -> RelayServer | None:
    """开通时用：池里这种 AI、现在能跑助理的实例中，所在节点上助理最少的一个（同数按编号）。"""
    relays = list(
        await session.scalars(
            select(RelayServer)
            .where(
                RelayServer.assistant_pool.is_(True),
                RelayServer.is_active.is_(True),
                RelayServer.model_provider == provider,
                RelayServer.runtime_node_id.is_not(None),
            )
            .order_by(RelayServer.id)
        )
    )
    if not relays:
        return None
    node_ids = {r.runtime_node_id for r in relays}
    found = await session.scalars(select(RuntimeNode).where(RuntimeNode.id.in_(node_ids)))
    nodes = {n.id: n for n in found}
    rows = await session.execute(
        select(RelayServer.runtime_node_id, func.count(Bot.id))
        .join(Bot, Bot.relay_server_id == RelayServer.id)
        .where(
            RelayServer.runtime_node_id.in_(node_ids),
            Bot.kind == "assistant",
            Bot.enabled.is_(True),
        )
        .group_by(RelayServer.runtime_node_id)
    )
    load: dict[uuid.UUID | None, int] = {node_id: count for node_id, count in rows.all()}
    ready = [
        r
        for r in relays
        if r.runtime_node_id is not None
        and unavailable_reason(r, nodes.get(r.runtime_node_id)) is None
    ]
    return min(ready, key=lambda r: (load.get(r.runtime_node_id, 0), str(r.id)), default=None)


async def hosts_standard_bots(session: AsyncSession, node_id: uuid.UUID) -> bool:
    """节点上有没有启用的普通员工。普通员工不在沙箱里、与助理是同一个系统用户，助理池节点不能有。"""
    count = await session.scalar(
        select(func.count(Bot.id))
        .join(RelayServer, Bot.relay_server_id == RelayServer.id)
        .where(
            RelayServer.runtime_node_id == node_id,
            Bot.kind == "standard",
            Bot.enabled.is_(True),
        )
    )
    return bool(count)


POOL_NODE_MESSAGE = "目标运行时是个人助理池的节点，不能承载普通员工"
SHARED_USER_MESSAGE = (
    "同一台机器上以同一个系统用户运行的另一个节点承载了普通员工或不在助理池里："
    "助理节点要用单独的系统用户安装"
)


async def is_pool_node(session: AsyncSession, node_id: uuid.UUID) -> bool:
    """节点上有没有实例在助理池里。"""
    found = await session.scalar(
        select(RelayServer.id)
        .where(RelayServer.runtime_node_id == node_id, RelayServer.assistant_pool.is_(True))
        .limit(1)
    )
    return found is not None


async def standard_bind_error(session: AsyncSession, relay: RelayServer) -> str | None:
    """普通员工绑定或换到这个实例时的校验：助理池节点上不能有普通员工。只在绑定与换运行时调用。"""
    if relay.runtime_node_id is not None and await is_pool_node(session, relay.runtime_node_id):
        return POOL_NODE_MESSAGE
    return None


async def pool_admission_error(
    session: AsyncSession, relay: RelayServer, node: RuntimeNode | None
) -> str | None:
    """实例能不能加入助理池；能加入返回 None。

    要求：共享（非私有）实例；节点声明了沙箱能力；节点上报了本机指纹与系统用户且不是 root；节点上
    没有普通员工；同一台机器上以同一个系统用户运行的其他节点也都是助理池节点、也没有普通员工。
    后两条保证助理节点与承载普通员工的守护进程是两个不同的系统用户：普通员工不在沙箱里，同一个
    用户就读得到助理这一轮的临时目录、转录与凭证副本。
    """
    if relay.visibility == "private":
        return "私有运行时不能加入助理池"
    if node is None or not node_sandboxed(node, relay.model_provider):
        return "该运行时还不能隔离运行个人助理，请先升级并确认节点自检通过"
    if not node.machine_fingerprint or node.uid is None:
        return "该节点没有上报本机指纹与系统用户，请先升级运行时"
    if node.uid == 0:
        return "助理节点不能以 root 运行"
    if await hosts_standard_bots(session, node.id):
        return "该节点上还有普通员工：助理池节点只承载个人助理"
    siblings = await session.scalars(
        select(RuntimeNode).where(
            RuntimeNode.machine_fingerprint == node.machine_fingerprint,
            RuntimeNode.uid == node.uid,
            RuntimeNode.id != node.id,
        )
    )
    for sibling in siblings:
        if await hosts_standard_bots(session, sibling.id) or not await is_pool_node(
            session, sibling.id
        ):
            return SHARED_USER_MESSAGE
    return None

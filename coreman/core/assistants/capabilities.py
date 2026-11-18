"""个人助理每轮的能力清单：本轮挂上了哪些业务系统（读写级别）、云签名代理放行了哪些产品。

进本轮块（`ctx.turn_notes`），不进稳定段：授权、运行时能力与在场与否每轮都可能变。个人凭证与
本人飞书的状态已经各有一行本轮说明（`chat/credentials.py`、`chat/personal.py`），签发失败、
所有者不在场与没人点名的说明在 `system_access.py`，这里都不重复。

级别分两种：本轮清单按这一轮实际下发了什么说（`build_system_access` 的 `delivered`）；缺口比对
按授权算的级别（`grant_rank`：0 没有、1 能读、2 能写），比报缺口时记下的高才算开通。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import gaps
from coreman.core.assistants.gaps import normalize_key
from coreman.core.cloud_proxy import aliyun
from coreman.core.cloud_proxy.risk_policy import CloudProxyPolicy
from coreman.core.db.models import (
    AssistantGap,
    Bot,
    BotSystemGrant,
    BusinessSystem,
    FeishuPersonalGrant,
)

Mode = Literal["attended", "unattended", "withheld"]
Level = Literal["confirm", "approval", "read", "unsupported", "withheld"]
LEVEL_TEXT: dict[str, str] = {
    "confirm": "可读写，写操作先经所有者确认",
    "approval": "只读，写操作由平台转所有者审批",
    "read": "只读",
    "unsupported": "本轮不可用（运行环境不支持）",
    "withheld": "本轮没下发，这一轮用不了",
}
HEAD = "- 你这一轮能用的（缺的权限用 gap_report 登记）："
# 冷启动归纳口吻、告警这类轮次：不是替所有者办事的一轮，清单不列，也不该在这一轮登记缺口。
WITHHELD = (
    HEAD + "这一轮不是替所有者办事的一轮，业务系统与云签名代理以本轮其他说明为准，不登记缺口。"
)
CLOUD_RULES = {
    "attended": "读直接调；写操作出确认卡，高风险的转审批",
    "unattended": "读直接调；写操作由平台转所有者审批",
}


class ProxyDeployment(Protocol):
    """云签名代理的部署配置：`Settings` 与 worker 的任务上下文都带着这两项。"""

    @property
    def cloud_proxy_ready(self) -> bool: ...

    @property
    def cloud_proxy_policy(self) -> CloudProxyPolicy | None: ...


@dataclass(frozen=True)
class SystemLine:
    key: str
    name: str
    level: Level


def level(
    system: BusinessSystem,
    allow_write: bool,
    *,
    mode: Mode,
    mcp_supported: bool,
    mounted: bool,
    delivered: bool = True,
) -> Level:
    """与 `system_access` 同一口径：代理系统要挂得上目录，写入看授权；持令牌直连的写靠确认。

    `delivered`：这一轮真的下发了（签出令牌、配上代理）；没有就是本轮没下发（没人点名、发言者
    身份不可用、签发失败、调度回合），不说可读写。
    """
    if system.token_delivery == "mcp" and not mcp_supported:
        return "unsupported"
    if system.token_delivery == "proxy" and not (mounted and system.openapi_url):
        return "unsupported"
    if not delivered:
        return "withheld"
    if system.token_delivery == "proxy":
        if not allow_write:
            return "read"
        return "approval" if mode == "unattended" else "confirm"
    return "read" if mode == "unattended" else "confirm"


def grant_rank(system: BusinessSystem, allow_write: bool) -> int:
    """按授权算的级别，不看这一轮：0 用不了、1 能读、2 能写（持令牌直连的系统令牌本身能写）。"""
    if system.token_delivery == "proxy":
        if not system.openapi_url:
            return 0
        return 2 if allow_write else 1
    return 2


async def _granted(session: AsyncSession, bot_id: uuid.UUID) -> list[tuple[BusinessSystem, bool]]:
    # 函数内引入：system_access 依赖提示词与目录模块，助理核心模块不在模块层引用它。
    from coreman.core.auth.system_access import granted_systems

    found = await granted_systems(session, bot_id)
    writes = set(
        await session.scalars(
            select(BotSystemGrant.system_key).where(
                BotSystemGrant.bot_id == bot_id, BotSystemGrant.allow_write
            )
        )
    )
    return [(system, system.key in writes) for system in found]


async def systems(
    session: AsyncSession,
    bot_id: uuid.UUID,
    *,
    mode: Mode,
    mcp_supported: bool,
    mounted: bool,
    delivered: frozenset[str],
) -> list[SystemLine]:
    return [
        SystemLine(
            system.key,
            " ".join((system.name or system.key).split()),
            level(
                system,
                write,
                mode=mode,
                mcp_supported=mcp_supported,
                mounted=mounted,
                delivered=system.key in delivered,
            ),
        )
        for system, write in await _granted(session, bot_id)
    ]


async def system_ranks(session: AsyncSession, bot_id: uuid.UUID) -> dict[str, int]:
    return {
        system.key: grant_rank(system, write) for system, write in await _granted(session, bot_id)
    }


def cloud_products(deployment: ProxyDeployment) -> list[str]:
    """部署放行的云产品：代理整体可用，且这个产品的 endpoint 配好了。"""
    if not deployment.cloud_proxy_ready:
        return []
    return sorted(
        key
        for key, product in aliyun.PRODUCTS.items()
        if aliyun.endpoint_for(product, deployment.cloud_proxy_policy) is not None
    )


async def current_rank(
    session: AsyncSession,
    bot_id: uuid.UUID,
    kind: str,
    key: str,
    *,
    deployment: ProxyDeployment | None = None,
) -> int:
    """报缺口时记下的级别（`gaps.report` 的 `seen_rank`）：业务系统按授权，云产品看部署放没放行。

    其余种类不比对级别（各有完成回调或助理自检），记 0。`deployment` 只在云产品时用，缺省读
    部署配置。

    Raises:
        GapError: invalid_kind / invalid_key
    """
    key = normalize_key(kind, key)
    if kind == "business_system":
        return (await system_ranks(session, bot_id)).get(key, 0)
    if kind == "cloud_product":
        if deployment is None:
            from coreman.core.config import get_settings

            deployment = get_settings()
        return 1 if key in cloud_products(deployment) else 0
    return 0


def note(lines: list[SystemLine], products: list[str], *, mode: Mode, cloud_attached: bool) -> str:
    if mode == "withheld":
        return WITHHELD
    parts = [
        "业务系统 "
        + "；".join(f"{line.name}（{line.key}）{LEVEL_TEXT[line.level]}" for line in lines)
        + "。"
        if lines
        else "业务系统：没有授权给你的。"
    ]
    if cloud_attached and products:
        parts.append(f"云签名代理：{'、'.join(products)}，{CLOUD_RULES[mode]}。")
    else:
        parts.append("云签名代理：本轮不可用。")
    return HEAD + "".join(parts)


async def _resolve_feishu(session: AsyncSession, bot_id: uuid.UUID) -> None:
    """所有者另行授权（如在「我的飞书」页）后，按他现在的飞书授权解决已满足的飞书权限缺口。

    没有未解决的飞书缺口时只做一次查询；失败只回滚这一段（`resolve_quietly`），不拖垮这一轮。
    """
    pending = await session.scalar(
        select(AssistantGap.id)
        .where(
            AssistantGap.bot_id == bot_id,
            AssistantGap.kind == "feishu_scope",
            AssistantGap.status.in_(gaps.LIVE),
        )
        .limit(1)
    )
    if pending is None:
        return
    grant = await session.scalar(
        select(FeishuPersonalGrant)
        .join(Bot, Bot.id == FeishuPersonalGrant.bot_id)
        .where(
            FeishuPersonalGrant.bot_id == bot_id,
            FeishuPersonalGrant.user_id == Bot.owner_id,
            FeishuPersonalGrant.status == "connected",
        )
    )
    if grant is None:
        return
    await gaps.resolve_quietly(
        session,
        bot_id,
        "feishu_scope",
        dict.fromkeys(gaps.feishu_keys(grant), 1),
        log_event="assistant_gap_resolve_failed_on_turn",
    )


async def apply(
    session: AsyncSession,
    bot_id: uuid.UUID,
    *,
    mode: Mode,
    mcp_supported: bool,
    mounted: bool,
    cloud_attached: bool,
    deployment: ProxyDeployment,
    delivered: frozenset[str] = frozenset(),
) -> str:
    """本轮说明；顺带把级别升上去了的业务系统与云产品缺口记为解决（按授权与部署，不看这一轮
    在不在场）。所有者在场的轮次还按他现在的飞书授权解决已满足的飞书权限缺口。冷启动、告警这类
    轮次不比对，等下一个正常的轮次。"""
    if mode == "withheld":
        return WITHHELD
    lines = await systems(
        session,
        bot_id,
        mode=mode,
        mcp_supported=mcp_supported,
        mounted=mounted,
        delivered=delivered,
    )
    products = cloud_products(deployment)
    await gaps.resolve(session, bot_id, "business_system", await system_ranks(session, bot_id))
    await gaps.resolve(session, bot_id, "cloud_product", dict.fromkeys(products, 1))
    if mode == "attended":
        await _resolve_feishu(session, bot_id)
    return note(lines, products, mode=mode, cloud_attached=cloud_attached)

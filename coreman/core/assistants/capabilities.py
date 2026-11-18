"""个人助理每轮的能力清单：本轮挂上了哪些业务系统（读写级别）、云签名代理放行了哪些产品。

进本轮块（`ctx.turn_notes`），不进稳定段：授权、运行时能力与在场与否每轮都可能变。个人凭证与
本人飞书的状态已经各有一行本轮说明（`chat/credentials.py`、`chat/personal.py`），签发失败、
所有者不在场与没人点名的说明在 `system_access.py`，这里都不重复。同一份清单也用来把已可用的
缺口记为解决。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import gaps
from coreman.core.cloud_proxy import aliyun
from coreman.core.cloud_proxy.risk_policy import CloudProxyPolicy
from coreman.core.db.models import BotSystemGrant, BusinessSystem

Mode = Literal["attended", "unattended", "withheld"]
Level = Literal["confirm", "approval", "read", "unsupported", "withheld"]
LEVEL_TEXT: dict[str, str] = {
    "confirm": "可读写，写操作先经所有者确认",
    "approval": "只读，写操作由平台转所有者审批",
    "read": "只读",
    "unsupported": "本轮不可用（运行环境不支持）",
    "withheld": "本轮没下发（这一轮没人点名）",
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
    tokenless: bool = False,
) -> Level:
    """与 `system_access` 同一口径：代理系统要挂得上目录，写入看授权；持令牌直连的写靠确认。

    `tokenless`：这一轮令牌会进运行环境的系统（env、mcp）不下发（没人点名的旁听插话、调度）。
    """
    if system.token_delivery == "mcp" and not mcp_supported:
        return "unsupported"
    if system.token_delivery == "proxy":
        if not (mounted and system.openapi_url):
            return "unsupported"
        if not allow_write:
            return "read"
        return "approval" if mode == "unattended" else "confirm"
    if tokenless:
        return "withheld"
    return "read" if mode == "unattended" else "confirm"


async def systems(
    session: AsyncSession,
    bot_id: uuid.UUID,
    *,
    mode: Mode,
    mcp_supported: bool,
    mounted: bool,
    tokenless: bool = False,
) -> list[SystemLine]:
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
    return [
        SystemLine(
            system.key,
            " ".join((system.name or system.key).split()),
            level(
                system,
                system.key in writes,
                mode=mode,
                mcp_supported=mcp_supported,
                mounted=mounted,
                tokenless=tokenless,
            ),
        )
        for system in found
    ]


def cloud_products(deployment: ProxyDeployment) -> list[str]:
    """部署放行的云产品：代理整体可用，且这个产品的 endpoint 配好了。"""
    if not deployment.cloud_proxy_ready:
        return []
    return sorted(
        key
        for key, product in aliyun.PRODUCTS.items()
        if aliyun.endpoint_for(product, deployment.cloud_proxy_policy) is not None
    )


def usable(lines: Iterable[SystemLine]) -> set[str]:
    """授权了、运行环境也支持的系统：这一轮没下发的也算（缺口按授权算，不看这一轮）。"""
    return {line.key for line in lines if line.level != "unsupported"}


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


async def apply(
    session: AsyncSession,
    bot_id: uuid.UUID,
    *,
    mode: Mode,
    mcp_supported: bool,
    mounted: bool,
    cloud_attached: bool,
    deployment: ProxyDeployment,
    tokenless: bool = False,
) -> str:
    """本轮说明；顺带把已经可用的业务系统与云产品缺口记为解决（按授权与部署，不看这一轮在不在场）。"""
    lines = await systems(
        session,
        bot_id,
        mode=mode,
        mcp_supported=mcp_supported,
        mounted=mounted,
        tokenless=tokenless,
    )
    products = cloud_products(deployment)
    await gaps.resolve(session, bot_id, "business_system", usable(lines))
    await gaps.resolve(session, bot_id, "cloud_product", products)
    return note(lines, products, mode=mode, cloud_attached=cloud_attached)

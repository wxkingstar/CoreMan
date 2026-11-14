"""State and helpers shared by the catalog tools and proxied calls within one verified MCP call."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth.token_providers import IssuedToken, TokenProviderError
from coreman.core.config import Settings
from coreman.core.db.models import BusinessSystem
from coreman.core.systems_catalog import service

TRUST = {"content_trust": "system_declared"}
# task.payload key holding this task's budget, permission cache and proxy tokens.
BUDGET_KEY = "systems_catalog"
# A failed permission lookup is retried after this long instead of on every call.
PERMISSIONS_RETRY_SECONDS = 60
PROXY_TOKEN_AAD = "systems_catalog.proxy_token.v1"
# Default variable the anonymous catalog's call templates read the caller's own credential from.
DIRECT_TOKEN_ENV = "SYSTEMS_TOKEN"
# A cached proxy token this close to expiry is replaced before use.
TOKEN_MARGIN_SECONDS = 30


@dataclass
class Context:
    """One verified call: the member, their systems this turn and the task's catalog state.

    `anonymous` is the read-only catalog with no member, task or tokens (`operator` is None): it
    serves stored catalogs only, never filters by permission, and the caller reaches the business
    system directly with its own credential.
    """

    session: AsyncSession
    issuance: service.Issuance
    operator: service.Operator | None
    systems: list[BusinessSystem]
    # Systems whose grant allows write-level proxied calls for this AI employee.
    writable: frozenset[str] = frozenset()
    # The owner approved writes for this turn in the approval system: they run without a card.
    writes_preapproved: bool = False
    # A personal assistant's turn with the owner absent (`kinds.unattended`): reads only.
    owner_absent: bool = False
    # Proxy tokens live as long as the AI employee's task timeout.
    token_ttl: int = 3600
    # Sections of task.payload[BUDGET_KEY]; the caller persists them when changed.
    perms: dict[str, Any] = field(default_factory=dict)
    tokens: dict[str, Any] = field(default_factory=dict)
    perms_changed: bool = False
    tokens_changed: bool = False
    anonymous: bool = False
    # Anonymous only: the caller's own environment variable named in its call templates.
    token_env: str = DIRECT_TOKEN_ENV
    # Large and file responses are kept in short-term object storage when set (see `spill`).
    settings: Settings | None = None

    def system(self, key: str) -> BusinessSystem | None:
        return next((s for s in self.systems if s.key == key), None)

    @property
    def member(self) -> service.Operator:
        """The verified member; only paths that issue tokens or record calls ask for it."""
        assert self.operator is not None
        return self.operator


def _aad(ctx: Context, system: BusinessSystem) -> str:
    return f"{PROXY_TOKEN_AAD}:{ctx.member.task_id}:{system.key}"


async def proxy_token(ctx: Context, system: BusinessSystem) -> IssuedToken:
    """The task's token for a proxied system: issued on first use, kept encrypted in the task.

    Raises TokenProviderError when it cannot be issued.
    """
    cipher = ctx.issuance.cipher
    cached = ctx.tokens.get(system.key)
    if (
        isinstance(cached, dict)
        and cached.get("expires_at", 0) - TOKEN_MARGIN_SECONDS > time.time()
    ):
        value = cipher.decrypt(cached["enc"], _aad(ctx, system))
        return IssuedToken(
            value,
            int(cached["expires_at"] - time.time()),
            int(cached["expires_at"]),
            cached["auth_mode"],
            cached.get("token_id"),
        )
    token = await service.issue_short_token(
        ctx.session,
        ctx.issuance,
        system,
        ctx.member,
        ttl_seconds=ctx.token_ttl,
        purpose="proxy",
    )
    await ctx.session.commit()
    ctx.tokens[system.key] = {
        "enc": cipher.encrypt(token.value, _aad(ctx, system)),
        "expires_at": token.expires_at,
        "auth_mode": token.auth_mode,
        "token_id": token.token_id,
    }
    ctx.tokens_changed = True
    return token


async def held(ctx: Context, system: BusinessSystem, loaded: service.Loaded) -> service.Held | None:
    """The member's permission codes for this system, cached in the task; None = no filtering."""
    if ctx.anonymous or not isinstance(loaded.compiled.get("permissions"), dict):
        return None
    cached = ctx.perms.get(system.key)
    if isinstance(cached, dict):
        if isinstance(cached.get("codes"), list):
            return service.Held(cached["codes"])
        if cached.get("until", 0) > time.time():
            return None
    codes: list[str] | None = None
    try:
        if system.token_delivery == "proxy":
            token = await proxy_token(ctx, system)
        else:
            token = await service.issue_short_token(ctx.session, ctx.issuance, system, ctx.member)
            await ctx.session.commit()
        codes = await service.held_permissions(loaded, system.base_url or "", token)
    except TokenProviderError:
        codes = None
    retry = time.time() + PERMISSIONS_RETRY_SECONDS
    ctx.perms[system.key] = {"codes": codes} if codes is not None else {"until": retry}
    ctx.perms_changed = True
    return service.Held(codes) if codes is not None else None


def permissions_unknown(ctx: Context, loaded: service.Loaded, codes: service.Held | None) -> bool:
    return (
        not ctx.anonymous and isinstance(loaded.compiled.get("permissions"), dict) and codes is None
    )


def visible(op: dict[str, Any], codes: service.Held | None) -> bool:
    return codes is None or codes.allows(op.get("permission") or [])


async def catalog(ctx: Context, system: BusinessSystem) -> service.Loaded | None:
    # The anonymous catalog has nobody to fetch as: it serves what tasks and admins last fetched.
    if not ctx.anonymous:
        await service.ensure_fresh(ctx.session, ctx.issuance, system, ctx.member)
    return await service.load(ctx.session, system.key)


def unavailable(system: BusinessSystem) -> dict[str, Any]:
    return {
        "error": "catalog_unavailable",
        "system": system.key,
        "message": "这个系统暂时没有可用的操作目录（未配置或拉取失败）。"
        "不要猜路径；请告知用户目录不可用，或按系统已有的说明处理。",
    }


def no_system(key: str) -> dict[str, Any]:
    return {
        "error": "system_not_available",
        "system": key,
        "message": "本轮没有这个业务系统的访问权限。不带参数调用 systems_browse 查看可用系统。",
    }


def operation_not_found(system: BusinessSystem, operation_id: str) -> dict[str, Any]:
    return {
        "error": "operation_not_found",
        "system": system.key,
        "operation_id": operation_id,
        "message": "目录中没有这个操作，或当前用户没有权限使用它。"
        "用 systems_search 查找，不要猜测路径或改用相近的操作。",
    }

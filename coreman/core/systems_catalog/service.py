"""Catalog storage, refresh and permission lookups.

A refresh fetches the description with a short token issued for the operator (the administrator
who saved the system, or the member whose task touched the catalog), so a system that refuses
anonymous reads still works and no standing credential is kept. Network calls never happen inside
an open transaction: each step commits before talking to the business system.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth.external_key import ExternalKey
from coreman.core.auth.provider_config import HTTPTokenProviderConfig
from coreman.core.auth.token_issues import IssuedRecord, record_issues
from coreman.core.auth.token_providers import (
    IssuedToken,
    TokenProviderError,
    issue_system_token,
)
from coreman.core.crypto import Cipher
from coreman.core.db.models import BusinessSystem, SystemCatalog, User
from coreman.core.logging import get_logger
from coreman.core.systems_catalog import fetch, loader
from coreman.core.systems_catalog.compiler import COMPILER_VERSION, compile_spec, same_origin
from coreman.core.systems_catalog.search import SearchIndex

log = get_logger(__name__)

RECHECK_AFTER = timedelta(minutes=10)
# An outdated compiler forces a full fetch, but a failing system is still tried at most this often.
RETRY_AFTER = timedelta(minutes=1)
FETCH_TOKEN_TTL = 300
PERMISSIONS_MAX_BYTES = 1024 * 1024
PERMISSIONS_TIMEOUT = 10
MAX_CODES = 5000
CACHE_SIZE = 32
PENDING = "pending"


@dataclass(frozen=True)
class Issuance:
    """Everything needed to issue a business token, taken from the API or worker process."""

    cipher: Cipher
    issuer: str
    external_key: ExternalKey | None
    providers: Mapping[str, HTTPTokenProviderConfig]


@dataclass(frozen=True)
class Operator:
    """Whose identity a short fetch token is issued for."""

    user: User
    subject: str
    task_id: int | None = None
    bot_id: Any = None


@dataclass(frozen=True)
class Loaded:
    system_key: str
    status: str
    sha: str
    compiled: dict[str, Any]
    index: SearchIndex


_cache: OrderedDict[tuple[str, str, int], Loaded] = OrderedDict()


def usable(status: str | None, has_compiled: bool) -> bool:
    return has_compiled and status in ("ok", "stale")


async def issue_short_token(
    session: AsyncSession,
    issuance: Issuance,
    system: BusinessSystem,
    operator: Operator,
    *,
    ttl_seconds: int = FETCH_TOKEN_TTL,
    purpose: str = "catalog",
) -> IssuedToken:
    token = await issue_system_token(
        session,
        issuance.cipher,
        system=system,
        subject=operator.subject,
        name=operator.user.display_name,
        ttl_seconds=ttl_seconds,
        issuer=issuance.issuer,
        external_key=issuance.external_key,
        providers=issuance.providers,
    )
    await record_issues(
        session,
        [
            IssuedRecord(
                system_key=system.key,
                provider=system.token_provider,
                audience=system.token_audience or system.key,
                token_id=token.token_id,
                expires_at=token.expires_at,
            )
        ],
        purpose=purpose,
        subject=operator.subject,
        task_id=operator.task_id,
        bot_id=operator.bot_id,
        user_id=operator.user.id,
    )
    return token


async def claim_recheck(session: AsyncSession, system: BusinessSystem) -> bool:
    """Single flight across processes: whoever moves `checked_at` forward does the check.

    A row is due when it was last checked over 10 minutes ago, points at an older URL, or was
    compiled by an older compiler. Commits, so others see the claim before the fetch starts.
    """
    now = datetime.now(UTC)
    claimed = await session.scalar(
        update(SystemCatalog)
        .where(
            SystemCatalog.system_key == system.key,
            or_(
                SystemCatalog.checked_at.is_(None),
                SystemCatalog.checked_at < now - RECHECK_AFTER,
                SystemCatalog.spec_url != system.openapi_url,
                (SystemCatalog.compiler_version < COMPILER_VERSION)
                & (SystemCatalog.checked_at < now - RETRY_AFTER),
            ),
        )
        .values(checked_at=func.now())
        .returning(SystemCatalog.system_key)
    )
    if claimed is None:
        claimed = await session.scalar(
            insert(SystemCatalog)
            .values(
                system_key=system.key,
                spec_url=system.openapi_url or "",
                status="error",
                error=PENDING,
                checked_at=func.now(),
            )
            .on_conflict_do_nothing()
            .returning(SystemCatalog.system_key)
        )
    await session.commit()
    return claimed is not None


async def refresh(
    session: AsyncSession,
    issuance: Issuance,
    system: BusinessSystem,
    operator: Operator,
    *,
    force: bool = False,
) -> SystemCatalog:
    """Fetch and compile now. Failures keep the previous catalog (status `stale`). Commits."""
    url = system.openapi_url or ""
    row = await session.get(SystemCatalog, system.key, populate_existing=True)
    if row is None:
        row = SystemCatalog(system_key=system.key, spec_url=url, status="error", error=PENDING)
        session.add(row)
    current = (
        row.spec_url == url
        and row.compiled is not None
        and row.compiler_version == COMPILER_VERSION
    )
    etag = row.etag if current and not force else None
    try:
        if not url or not system.base_url:
            raise fetch.FetchError("not_configured")
        token = await issue_short_token(session, issuance, system, operator)
        await session.commit()
        fetched = await fetch.fetch_spec(url, base_url=system.base_url, token=token, etag=etag)
        now = datetime.now(UTC)
        if fetched.not_modified:
            row.checked_at, row.status, row.error = now, "ok", None
        else:
            digest = hashlib.sha256(fetched.body).hexdigest()
            if not (current and digest == row.spec_sha256):
                result = compile_spec(
                    loader.load(fetched.body), spec_url=url, base_url=system.base_url
                )
                row.compiled = result.compiled
                row.lint = result.lint
                row.operation_count = result.operation_count
                row.hidden_count = result.hidden_count
                row.module_count = result.module_count
                row.compiler_version = COMPILER_VERSION
                row.spec_sha256 = digest
            row.spec_url, row.etag, row.spec_bytes = url, fetched.etag, len(fetched.body)
            row.fetched_at = row.checked_at = now
            row.status, row.error = "ok", None
    except (fetch.FetchError, loader.SpecError, TokenProviderError) as exc:
        row.checked_at = datetime.now(UTC)
        keep = row.compiled is not None and row.spec_url == url
        row.status, row.error = ("stale" if keep else "error"), exc.code
        if not keep:
            row.spec_url, row.compiled, row.etag = url, None, None
        log.info("system_catalog_refresh_failed", system=system.key, error=exc.code)
    await session.commit()
    return row


async def ensure_fresh(
    session: AsyncSession, issuance: Issuance, system: BusinessSystem, operator: Operator
) -> None:
    if system.openapi_url and system.base_url and await claim_recheck(session, system):
        await refresh(session, issuance, system, operator)


async def heads(session: AsyncSession, keys: list[str]) -> dict[str, tuple[str, int, int, bool]]:
    """Light per-system summary for prompts: (status, module_count, operation_count, usable)."""
    if not keys:
        return {}
    rows = await session.execute(
        select(
            SystemCatalog.system_key,
            SystemCatalog.status,
            SystemCatalog.module_count,
            SystemCatalog.operation_count,
            SystemCatalog.compiled.is_not(None),
        ).where(SystemCatalog.system_key.in_(keys))
    )
    return {
        key: (status, modules, count, usable(status, bool(has)))
        for key, status, modules, count, has in rows.all()
    }


async def load(session: AsyncSession, system_key: str) -> Loaded | None:
    """The usable catalog with its search index, from the in-process cache when unchanged."""
    head = (
        await session.execute(
            select(
                SystemCatalog.status,
                SystemCatalog.spec_sha256,
                SystemCatalog.compiler_version,
                SystemCatalog.compiled.is_not(None),
            ).where(SystemCatalog.system_key == system_key)
        )
    ).first()
    if head is None or not usable(head[0], bool(head[3])) or not head[1]:
        return None
    status, sha, version = head[0], str(head[1]), int(head[2])
    key = (system_key, sha, version)
    cached = _cache.get(key)
    if cached is not None:
        _cache.move_to_end(key)
        return Loaded(system_key, status, sha, cached.compiled, cached.index)
    compiled = await session.scalar(
        select(SystemCatalog.compiled).where(SystemCatalog.system_key == system_key)
    )
    if not isinstance(compiled, dict):
        return None
    loaded = Loaded(system_key, status, sha, compiled, SearchIndex(compiled))
    _cache[key] = loaded
    while len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)
    return loaded


def resolve_pointer(document: Any, pointer: str) -> Any:
    node = document
    for raw in pointer.split("/")[1:]:
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


async def held_permissions(loaded: Loaded, base_url: str, token: IssuedToken) -> list[str] | None:
    """The caller's permission codes from the system's declared lookup, or None if unknown."""
    spec = loaded.compiled.get("permissions")
    if not isinstance(spec, dict):
        return None
    target = str(loaded.compiled.get("server") or base_url) + str(spec["path"])
    if not same_origin(target, base_url):
        return None
    url = httpx.URL(target)
    try:
        async with fetch.client_for(url, timeout=PERMISSIONS_TIMEOUT) as client:
            async with client.stream(
                "GET", url, headers={"Accept": "application/json", **fetch.auth_headers(token)}
            ) as response:
                if response.status_code != 200:
                    return None
                raw = bytearray()
                async for part in response.aiter_bytes():
                    raw.extend(part)
                    if len(raw) > PERMISSIONS_MAX_BYTES:
                        return None
        codes = resolve_pointer(json.loads(raw), str(spec["pointer"]))
    except (fetch.FetchError, httpx.HTTPError, TimeoutError, ValueError):
        return None
    if not isinstance(codes, list) or len(codes) > MAX_CODES:
        return None
    return [c for c in codes if isinstance(c, str) and 0 < len(c) <= 200]


class Held:
    """Permission codes held by the caller, with the contract's matching rules."""

    def __init__(self, codes: list[str]) -> None:
        self.all = "*" in codes
        self.exact = set(codes)
        self.prefixes = tuple(c[:-1] for c in codes if c.endswith(":*"))

    def covers(self, code: str) -> bool:
        return self.all or code in self.exact or code.startswith(self.prefixes)

    def allows(self, required: list[str]) -> bool:
        return all(self.covers(code) for code in required)

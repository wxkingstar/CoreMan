"""个人助理的能力缺口：办不成事时卡在哪个权限上、所有者怎么开、开通后自动续接。

一条缺口按 (助理, 种类, 标识) 唯一：同一个缺口卡住几件事，就有几条待跟进的 `gap_id` 指向它。
`need_enc`（要它做什么）登记在 `sealed.COLUMNS`；种类、标识、状态是明文，标识只能是代号。

「怎么开」（`fix_for`）读取时按种类判定，不由助理填、也不落库。缺口变成可用（`resolve`）时，
被它卡住的未结待跟进刷新进展时间，并入一条立即级别的提醒，下一轮交回时告诉助理哪几件可以继续；
同一次解决只入一条。所有者「不开」（`decline`）的缺口不再进总表，助理再报同一个缺口得到
`declined`。只 flush，不提交；日志只有编号与计数。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import followups, sealed
from coreman.core.db.models import GAP_KINDS, AssistantFollowup, AssistantGap, BusinessSystem
from coreman.core.logging import get_logger
from coreman.core.timeutils import utcnow

log = get_logger(__name__)

LIVE = ("open", "requested")
MAX_LIVE = 100
MAX_NEED = 200
# 只有这两种平台检测不到，由助理自检后自报已解决。
SELF_RESOLVABLE = ("node_tool", "other")
KIND_TITLES = {
    "feishu_scope": "飞书权限",
    "business_system": "业务系统",
    "cloud_product": "云签名代理产品",
    "credential": "个人凭证",
    "node_tool": "节点工具",
    "other": "其他",
}
FIXES = ("authorize", "credential", "grant_page", "admin", "node_admin", "none")
# 在与助理的私聊里当面办得了的：卡片上给「开通」按钮。
CHAT_FIXES = ("authorize", "credential")
_LABEL = re.compile(r"^[Gg]?(\d{1,9})$")
_KEY = re.compile(r"^[a-z0-9][a-z0-9_.:/\-]{0,99}$")
_CREDENTIAL = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_HINTS = {
    "authorize": "在与助理的私聊里点「开通 {label}」，再点授权卡片",
    "credential": "在与助理的私聊里点「开通 {label}」，在安全表单里填写（不经聊天）",
    "grant_page": "你可以自己开：在「AI 员工」里这位助理的「系统权限」中勾选 {key}",
    "admin": "需要平台管理员为这位助理开放{title} {key}",
    "node_admin": "需要节点管理员在运行节点上装好 {key}",
    "none": "助理会在备注里写明要怎么办",
}


class GapError(Exception):
    """调用方能照着改的拒绝；`code` 原样作为工具的 error 返回，不带任何内容。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def label(seq: int) -> str:
    return f"G{seq:03d}"


def parse_label(value: str) -> int:
    match = _LABEL.match(value.strip())
    if match is None or int(match.group(1)) < 1:
        raise GapError("gap_not_found")
    return int(match.group(1))


def normalize_key(kind: str, key: str) -> str:
    """凭证是环境变量名（大写，去掉 `$`）；其余一律小写代号。

    Raises:
        GapError: invalid_kind / invalid_key
    """
    if kind not in GAP_KINDS:
        raise GapError("invalid_kind")
    clean = key.strip().lstrip("$")
    if kind == "credential":
        clean = clean.upper()
        ok = _CREDENTIAL.fullmatch(clean)
    else:
        clean = clean.lower()
        ok = _KEY.fullmatch(clean)
    if not ok:
        raise GapError("invalid_key")
    return clean


def _keys(kind: str, keys: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for key in keys:
        try:
            out.add(normalize_key(kind, key))
        except GapError:
            continue
    return out


async def _lock(session: AsyncSession, bot_id: uuid.UUID) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"assistant-gaps:{bot_id}"},
    )


async def get(
    session: AsyncSession, bot_id: uuid.UUID, seq: int, *, lock: bool = False
) -> AssistantGap:
    """这位助理的第 `seq` 个缺口，内容已还原（session.scalar）。

    Raises:
        GapError: gap_not_found
    """
    stmt = select(AssistantGap).where(AssistantGap.bot_id == bot_id, AssistantGap.seq == seq)
    if lock:
        stmt = stmt.with_for_update()
    row: AssistantGap | None = await session.scalar(stmt)
    if row is None:
        raise GapError("gap_not_found")
    return row


async def stuck(session: AsyncSession, bot_id: uuid.UUID, gap_id: uuid.UUID) -> list[int]:
    """被这个缺口卡住、还未结的待跟进编号。只读元数据。"""
    return list(
        await session.scalars(
            select(AssistantFollowup.seq)
            .where(
                AssistantFollowup.bot_id == bot_id,
                AssistantFollowup.gap_id == gap_id,
                AssistantFollowup.status.in_(followups.LIVE),
            )
            .order_by(AssistantFollowup.seq)
        )
    )


async def _tell(
    session: AsyncSession, bot_id: uuid.UUID, row: AssistantGap, kind: str, *, level: str
) -> None:
    """被它卡住的事刷新进展，并入一条提醒交回助理；没有卡住的事就什么都不做。"""
    seqs = await stuck(session, bot_id, row.id)
    if not seqs:
        return
    # 函数内引入：delivery_checks 依赖面大，模块层引入会与总表、摘要成环。
    from coreman.core.assistants import delivery_checks

    now = utcnow()
    await followups.touch(session, bot_id, seqs)
    await delivery_checks.add_reminder(
        session,
        bot_id,
        f"gap:{row.id}:{kind}:{int(now.timestamp())}",
        level=level,
        now=now,
        summary={
            "reminder": kind,
            "gap": label(row.seq),
            "kind": row.kind,
            "key": row.key,
            "followups": [followups.label(seq) for seq in seqs],
        },
    )


async def _resolve_rows(session: AsyncSession, bot_id: uuid.UUID, rows: list[AssistantGap]) -> None:
    now = utcnow()
    for row in rows:
        row.status = "resolved"
        row.resolved_at = row.updated_at = now
    await session.flush()
    for row in rows:
        await _tell(session, bot_id, row, "gap_resolved", level="immediate")
    log.info("assistant_gaps_resolved", bot_id=str(bot_id), gaps=len(rows))


async def report(
    session: AsyncSession,
    bot_id: uuid.UUID,
    *,
    kind: str,
    key: str,
    need: str,
    followup_seqs: list[int],
    resolved: bool = False,
) -> tuple[AssistantGap, str]:
    """登记（或补充）一个缺口，把这几条待跟进挂上去；`resolved` 是助理自检后报已解决。

    Returns:
        (缺口, created | updated | declined | resolved)。declined：所有者不开，原样返回，
        不挂待跟进，也不再入提醒。

    Raises:
        GapError: invalid_kind / invalid_key / invalid_need / resolve_not_allowed / gap_not_found /
            too_many_gaps / content_locked
        FollowupError: followup_not_found / followup_closed
    """
    key = normalize_key(kind, key)
    need_text = " ".join(need.split())
    if not need_text or len(need_text) > MAX_NEED:
        raise GapError("invalid_need")
    if resolved and kind not in SELF_RESOLVABLE:
        raise GapError("resolve_not_allowed")
    await _lock(session, bot_id)
    items: list[AssistantFollowup] = []
    for seq in dict.fromkeys(followup_seqs):
        item = await followups.get(session, bot_id, seq, lock=True)
        if item.status not in followups.LIVE:
            raise followups.FollowupError("followup_closed")
        items.append(item)
    row: AssistantGap | None = await session.scalar(
        select(AssistantGap)
        .where(AssistantGap.bot_id == bot_id, AssistantGap.kind == kind, AssistantGap.key == key)
        .with_for_update()
    )
    if resolved:
        if row is None:
            raise GapError("gap_not_found")
        if row.status in LIVE:
            await _resolve_rows(session, bot_id, [row])
        return row, "resolved"
    if row is not None and row.status == "declined":
        return row, "declined"
    if not await sealed.key_ready(session, bot_id):
        raise GapError("content_locked")
    now = utcnow()
    if row is None:
        live = await session.scalar(
            select(func.count())
            .select_from(AssistantGap)
            .where(AssistantGap.bot_id == bot_id, AssistantGap.status.in_(LIVE))
        )
        if (live or 0) >= MAX_LIVE:
            raise GapError("too_many_gaps")
        last = await session.scalar(
            select(func.max(AssistantGap.seq)).where(AssistantGap.bot_id == bot_id)
        )
        row = AssistantGap(
            bot_id=bot_id,
            seq=(last or 0) + 1,
            kind=kind,
            key=key,
            need_enc=need_text,
            status="open",
        )
        session.add(row)
        await session.flush()
        outcome = "created"
    else:
        row.need_enc = need_text
        if row.status == "resolved":
            # 用过又不行了（授权被收回、系统被撤）：重新打开。
            row.status, row.resolved_at = "open", None
        row.updated_at = now
        outcome = "updated"
    for item in items:
        item.gap_id = row.id
        item.progress_at = item.updated_at = now
    await session.flush()
    return row, outcome


async def resolve(
    session: AsyncSession, bot_id: uuid.UUID, kind: str, keys: Iterable[str]
) -> list[AssistantGap]:
    """这些标识现在可用了：这位助理这一种类里对得上的未解决缺口记为已解决，并交回续接。

    不认识的标识跳过；已经解决或不开的不动。返回这一次解决的缺口。
    """
    wanted = _keys(kind, keys)
    if not wanted:
        return []
    await _lock(session, bot_id)
    rows = list(
        await session.scalars(
            select(AssistantGap)
            .where(
                AssistantGap.bot_id == bot_id,
                AssistantGap.kind == kind,
                AssistantGap.key.in_(sorted(wanted)),
                AssistantGap.status.in_(LIVE),
            )
            .order_by(AssistantGap.seq)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if not rows:
        return []
    await sealed.open_rows(session, rows)
    await _resolve_rows(session, bot_id, rows)
    return rows


async def decline(session: AsyncSession, bot_id: uuid.UUID, seq: int) -> AssistantGap:
    """所有者明确不开：不再进总表，被卡住的事回到原来的阶段，助理得知这条路走不通。

    Raises:
        GapError: gap_not_found / gap_resolved
    """
    await _lock(session, bot_id)
    row = await get(session, bot_id, seq, lock=True)
    if row.status == "declined":
        return row
    if row.status == "resolved":
        raise GapError("gap_resolved")
    now = utcnow()
    row.status = "declined"
    row.declined_at = row.updated_at = now
    await session.flush()
    await _tell(session, bot_id, row, "gap_declined", level="batch")
    log.info("assistant_gap_declined", bot_id=str(bot_id), gap=label(row.seq))
    return row


async def listing(
    session: AsyncSession, bot_id: uuid.UUID, statuses: Iterable[str]
) -> list[tuple[AssistantGap, list[int]]]:
    """这几种状态的缺口（内容已还原）与各自卡住的未结待跟进；卡住得多的在前。"""
    rows = list(
        await session.scalars(
            select(AssistantGap)
            .where(AssistantGap.bot_id == bot_id, AssistantGap.status.in_(list(statuses)))
            .order_by(AssistantGap.seq)
            .execution_options(populate_existing=True)
        )
    )
    await sealed.open_rows(session, rows)
    out = [(row, await stuck(session, bot_id, row.id)) for row in rows]
    out.sort(key=lambda pair: (-len(pair[1]), pair[0].seq))
    return out


async def fix_for(session: AsyncSession, bot_id: uuid.UUID, row: AssistantGap) -> str:
    """怎么开。业务系统：对这位助理开放的（白名单为空表示全开放）所有者自己就能在系统权限里勾选，
    其余（没登记、停用、保留标识、白名单里没有它）要平台管理员。"""
    if row.kind == "feishu_scope":
        return "authorize"
    if row.kind == "credential":
        return "credential"
    if row.kind == "node_tool":
        return "node_admin"
    if row.kind == "other":
        return "none"
    if row.kind == "cloud_product":
        return "admin"
    from coreman.core.auth.token_providers import RESERVED_SYSTEM_KEYS

    system = await session.get(BusinessSystem, row.key)
    if system is None or not system.enabled or system.key in RESERVED_SYSTEM_KEYS:
        return "admin"
    if system.allowed_bot_ids is None or bot_id in system.allowed_bot_ids:
        return "grant_page"
    return "admin"


def hint(row: AssistantGap, fix: str) -> str:
    """给所有者看的「怎么开」，一句话。"""
    return _HINTS[fix].format(label=label(row.seq), key=row.key, title=KIND_TITLES[row.kind])


def view(row: AssistantGap, fix: str, seqs: list[int]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": label(row.seq),
        "kind": row.kind,
        "key": row.key,
        "need": row.need_enc,
        "status": row.status,
        "fix": fix,
        "hint": hint(row, fix),
        "followups": [followups.label(seq) for seq in seqs],
    }
    if sealed.is_locked(row):
        out["locked"] = True
    return out


async def view_of(session: AsyncSession, bot_id: uuid.UUID, row: AssistantGap) -> dict[str, Any]:
    return view(row, await fix_for(session, bot_id, row), await stuck(session, bot_id, row.id))

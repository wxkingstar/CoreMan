"""A member's own agent's write, approved in Tower instead of on a Feishu card.

When the member used Tower within `ACTIVE_WITHIN`, the staged write (`member_writes`) is first sent
to Tower as an approval request for the member themself, like the personal assistant's writes:
approved → a one-time token is fetched and redeemed against the exact staged request, then the
write runs once (`runtime/worker/member_write_tower.py`). Otherwise, or when Tower can't take it,
the Feishu card is sent as before.

Only one route can ever approve a write. A request nobody answered in Tower within the configured
wait (`WAIT_SETTING`) is withdrawn there first; only once Tower confirms the withdrawal is the
Feishu card sent. If the withdrawal is refused (already decided) the card is never sent and
Tower's decision stands; while Tower can't be reached, the next tick tries again.

The activity lookup (`GET /v1/users/{username}/last-active`) may be missing (404), down or slow:
all of that counts as not active and the card is sent.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants.approvals import tower
from coreman.core.bus import tasks
from coreman.core.config import get_settings
from coreman.core.crypto import Cipher
from coreman.core.db.models import InteractionState, Setting
from coreman.core.logging import get_logger
from coreman.core.systems_catalog import confirm
from coreman.core.timeutils import aware_utc, utcnow

log = get_logger(__name__)

# Tower's target system for these requests; its key goes into `TOWER_TARGET_KEYS`.
TARGET = "coreman-member-write"
EXTERNAL_PREFIX = "cm-mw-"
# A member who used Tower this recently gets their writes there.
ACTIVE_WITHIN = timedelta(days=3)
# The activity lookup sits in the agent's call: keep it short, a slow Tower means the card.
LOOKUP_TIMEOUT = 3.0
# Minutes a Tower request may go unanswered before it is withdrawn and the card sent instead.
WAIT_SETTING = "member_write_tower_wait_minutes"
WAIT_DEFAULT = 360
# The Tower request lives this much longer than the wait, so it is withdrawn before it expires.
MARGIN = timedelta(hours=1)
EXECUTE_KIND = "member_write_execute"
FALLBACK_BATCH = 20
KIND = confirm.MEMBER_KIND


def external_id(state_id: uuid.UUID) -> str:
    return EXTERNAL_PREFIX + state_id.hex


def state_id_of(external: str) -> uuid.UUID | None:
    if not external.startswith(EXTERNAL_PREFIX):
        return None
    raw = external.removeprefix(EXTERNAL_PREFIX)
    try:
        state_id = uuid.UUID(hex=raw)
    except ValueError:
        return None
    return state_id if state_id.hex == raw else None


def request_url(cfg: tower.Config, external: str) -> str:
    """The request's nominal address: the platform never calls it, it is hashed and shown."""
    return f"{cfg.public_base_url}/api/systems/member-writes/{external}/execute"


def request_body(item: dict[str, Any]) -> bytes:
    """The staged request in canonical form; submit and redeem both compute it from the stored
    item, so a redeemed token proves this is the request that was approved."""
    return json.dumps(
        {
            "system": item["system"],
            "operation_id": item["operation_id"],
            "method": item["method"],
            "path": item["path"],
            "path_params": item.get("path_params"),
            "query": item.get("query"),
            "body": item.get("body"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()


def request_hash(cfg: tower.Config, state: InteractionState, item: dict[str, Any]) -> str:
    external = str(state.state["external_id"])
    return tower.request_hash(
        cfg,
        external,
        request_body(item),
        str(state.state["caller"]),
        url=request_url(cfg, external),
    )


def config() -> tower.Config | None:
    return tower.config(get_settings(), TARGET)


async def wait_of(session: AsyncSession) -> timedelta:
    """The configured wait before falling back to the card (`WAIT_SETTING`)."""
    row = await session.get(Setting, WAIT_SETTING)
    try:
        minutes = int(row.value) if row is not None else WAIT_DEFAULT
    except (TypeError, ValueError):
        minutes = WAIT_DEFAULT
    return timedelta(minutes=max(minutes, 1))


def _keep(state: InteractionState) -> datetime | None:
    staged = state.state.get("tower_until")
    if not isinstance(staged, str):
        return None
    return aware_utc(datetime.fromisoformat(staged)) + confirm.KEEP


async def submit(
    state: InteractionState,
    item: dict[str, Any],
    *,
    username: str,
    caller: str,
    client: str,
    now: datetime,
    wait: timedelta,
) -> bool:
    """Send the staged write to Tower when the member uses it; False means send the card.

    Not configured, no login name, not active within `ACTIVE_WITHIN`, the lookup failing or Tower
    refusing the request: all False, nothing raised."""
    cfg = config()
    if cfg is None or not username:
        return False
    seen = await tower.last_active(cfg, username, wait_seconds=LOOKUP_TIMEOUT)
    if seen is None or now - seen > ACTIVE_WITHIN:
        return False
    external = external_id(state.id)
    expires_at = now + wait + MARGIN
    system = item.get("system_name") or item["system"]
    headline = confirm.headline(1, item, numbered=False)
    try:
        sent = await tower.submit(
            cfg,
            external_id=external,
            requester=username,
            caller=caller,
            abstract=confirm._plain(f"{system}：{headline}", 120),
            content=f"来自你本机的 {confirm._plain(client, 40)}（客户端自报名称）\n\n"
            + confirm.card_text([item]),
            body=request_body(item),
            expires_at=expires_at,
            url=request_url(cfg, external),
        )
    except tower.TowerError as exc:
        log.warning("member_write_tower_submit_failed", state=str(state.id), code=exc.code)
        return False
    state.state = {
        **state.state,
        "route": "tower",
        "external_id": external,
        "approval_id": sent.approval_id,
        "tower_at": now.isoformat(),
        "tower_until": expires_at.isoformat(),
        "caller": caller,
        "requester": username,
    }
    state.expires_at = expires_at
    return True


async def decide(
    session: AsyncSession,
    *,
    approval_id: str,
    external_id: str,
    status: str,
    reason: str | None,
    decided_at: datetime | None,
) -> tuple[bool, bool]:
    """Tower's decision on a member write; shared by the callback and reconciliation. Returns
    (known, queued). Only an open request still on the Tower route moves, so replays, late
    decisions and decisions after a withdrawal change nothing. Callers commit."""
    state_id = state_id_of(external_id)
    state = (
        await session.scalar(
            select(InteractionState)
            .where(InteractionState.id == state_id, InteractionState.kind == KIND)
            .with_for_update()
        )
        if state_id
        else None
    )
    if (
        state is None
        or state.state.get("external_id") != external_id
        or state.state.get("approval_id") not in (None, approval_id)
    ):
        return False, False
    if state.state.get("route") != "tower" or state.status != "open":
        return True, False
    when = (decided_at or utcnow()).isoformat()
    state.expires_at = _keep(state)
    if status == "approved":
        # `submitted` without `sending_at`: approved, waiting for the execute task (`runnable`).
        state.status = "submitted"
        state.state = {**state.state, "decided_at": when, "tower_status": "approved"}
        task = await tasks.enqueue(
            session,
            tasks.NewTask(
                bot_id=state.bot_id,
                kind=EXECUTE_KIND,
                dedupe_key=f"member-write:{state.id}:execute",
                payload={"state_id": str(state.id)},
            ),
        )
        return True, task is not None
    note = f"（理由：{confirm._plain(reason, 200)}）" if reason else ""
    outcome = (
        {"status": "rejected", "message": f"在 Tower 被拒绝了{note}，没有执行。不要重试。"}
        if status == "rejected"
        else {"status": "expired", "message": "Tower 审批单已过期，没有执行。需要的话重新提交。"}
    )
    state.status = "cancelled" if status == "rejected" else "expired"
    state.state = {**state.state, "decided_at": when, "result": outcome}
    return True, False


def runnable(state: InteractionState) -> bool:
    """Approved in Tower and not started: the execute task may run it, once."""
    staged = state.state
    return (
        state.status == "submitted"
        and staged.get("route") == "tower"
        and staged.get("tower_status") == "approved"
        and "sending_at" not in staged
        and "result" not in staged
    )


async def fall_back(session: AsyncSession, now: datetime, cipher: Cipher, wait: timedelta) -> int:
    """Requests unanswered in Tower for `wait`: withdraw them there, then send the Feishu card,
    valid `confirm.TTL` from now. Returns how many moved to the card. Callers commit."""
    from coreman.core.systems_catalog import member_writes  # member_writes 会调本模块

    cfg = config()
    if cfg is None:
        # Without the configuration nothing can be withdrawn: the request stays with Tower.
        return 0
    rows = list(
        await session.scalars(
            select(InteractionState)
            .where(
                InteractionState.kind == KIND,
                InteractionState.status == "open",
                InteractionState.state["route"].astext == "tower",
                InteractionState.state["fallback_refused"].is_(None),
                InteractionState.expires_at > now,
            )
            .order_by(InteractionState.expires_at)
            .limit(FALLBACK_BATCH)
            .with_for_update(skip_locked=True)
        )
    )
    moved = 0
    for state in rows:
        started = aware_utc(datetime.fromisoformat(str(state.state["tower_at"])))
        if now - started < wait:
            continue
        try:
            await tower.withdraw(cfg, str(state.state["external_id"]))
        except tower.TowerError as exc:
            if exc.unavailable:
                continue
            # Already decided (or unknown to Tower): never also offer the card.
            state.state = {**state.state, "fallback_refused": exc.code}
            log.info("member_write_tower_withdraw_refused", state=str(state.id), code=exc.code)
            continue
        state.state = {**state.state, "route": "feishu", "withdrawn_at": now.isoformat()}
        [item] = member_writes.unseal(cipher, state)
        await confirm.send_card(session, state, member_writes.card_body(state, item), now=now)
        moved += 1
    return moved

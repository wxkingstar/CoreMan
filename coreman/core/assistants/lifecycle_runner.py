"""看护进程推进开通与停用：每一步一个事务，失败原地重试，放弃时收拾干净。

由 scheduler 单独的一条循环调用（不在与普通员工共用的那一轮看护里）：这里有对节点与飞书的网络调用，
慢了、卡了、出错了都只影响个人助理的开通与停用。每一步另有 `STEP_SECONDS` 的总时限。

一轮做四件事：结掉过期没点的开通卡片；把账号已停用的成员的助理送进停用（先宽限）；推进到期的流程；
删一批已登记的旧工作目录。给本人的消息在那一步提交之后才发，发不出去只记一笔：例如授权刚完成，
不能因为欢迎语没发出去就把这一步（和刚拿到的令牌）回滚掉。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.assistants import deprovisioning, keys, provisioning, retired_dirs
from coreman.core.assistants.lifecycle import (
    PROVISION_MAX_ATTEMPTS,
    Messenger,
    StepFailed,
    StepRetry,
    backoff,
    finish,
)
from coreman.core.crypto import Cipher
from coreman.core.db.models import AssistantLifecycle, Bot, User
from coreman.core.logging import get_logger

log = get_logger(__name__)
STEP_SECONDS = 150
BATCH = 10
DEPARTURE_BATCH = 20
# 一条通知：（应用、飞书 user_id、流程编号、文本）。
Notice = tuple[uuid.UUID, str, uuid.UUID, str]


async def _expire_offers(factory: async_sessionmaker[AsyncSession], now: datetime) -> int:
    async with factory() as session:
        result = await session.execute(
            update(AssistantLifecycle)
            .where(
                AssistantLifecycle.finished_at.is_(None),
                AssistantLifecycle.action == "provision",
                AssistantLifecycle.step == "offered",
                AssistantLifecycle.next_attempt_at.is_(None),
                AssistantLifecycle.deadline < now,
            )
            .values(finished_at=now, outcome="expired", offer_hash=None, updated_at=now)
        )
        await session.commit()
    return int(getattr(result, "rowcount", 0) or 0)


async def _departures(factory: async_sessionmaker[AsyncSession]) -> int:
    """账号已停用的成员：他的助理进入停用（宽限期内账号恢复就作罢）。"""
    async with factory() as session:
        bot_ids = list(
            await session.scalars(
                select(Bot.id)
                .join(User, User.id == Bot.owner_id)
                .where(
                    Bot.kind == "assistant",
                    Bot.enabled.is_(True),
                    Bot.assistant_state == "active",
                    User.status == "disabled",
                )
                .limit(DEPARTURE_BATCH)
            )
        )
    started = 0
    for bot_id in bot_ids:
        try:
            async with factory() as session:
                bot = await session.get(Bot, bot_id)
                if bot is not None:
                    await deprovisioning.request(session, bot, trigger="member_left", actor_id=None)
                    await session.commit()
                    started += 1
        except Exception as exc:  # noqa: BLE001 一位出错不挡别人
            log.warning("assistant_departure_failed", error=type(exc).__name__)
    return started


async def _give_up(session: AsyncSession, row: AssistantLifecycle, code: str) -> list[str]:
    """这一步再也做不成：开通就此放弃并把已建的东西送去停用；停用（只会是记录没了）直接销毁密钥。"""
    if row.action == "deprovision":
        await keys.destroy_key(session, row.owner_id)
        finish(row, "failed", code)
        return []
    finish(row, "failed", code)
    bot = await session.get(Bot, row.bot_id) if row.bot_id else None
    if bot is not None and bot.assistant_state == "provisioning":
        await session.flush()
        await deprovisioning.request(session, bot, trigger="abandoned", actor_id=None)
    return [provisioning.failure_text(code)]


async def _settle_failure(
    factory: async_sessionmaker[AsyncSession],
    row_id: uuid.UUID,
    code: str,
    *,
    permanent: bool,
    now: datetime,
) -> list[str]:
    """步骤的事务已回滚：另开一个事务记下失败，或（再也做不成时）放弃。"""
    async with factory() as session:
        row = await session.get(AssistantLifecycle, row_id, with_for_update=True)
        if row is None or row.finished_at is not None:
            return []
        row.attempts += 1
        row.error_code, row.updated_at = code, now
        if permanent or (row.action == "provision" and row.attempts >= PROVISION_MAX_ATTEMPTS):
            texts = await _give_up(session, row, code)
        else:
            row.next_attempt_at = now + backoff(row.attempts)
            texts = []
        await session.commit()
        return texts


async def _push_back(
    factory: async_sessionmaker[AsyncSession], row_id: uuid.UUID, code: str, now: datetime
) -> None:
    """`_run_one` 本身出了错（未知步骤、回滚失败、记失败也失败）：最后兜底把这条流程往后推，
    免得它每一轮都排在最前面、挤掉别的流程。正被别人处理的行不等它。"""
    try:
        async with factory() as session:
            row = await session.scalar(
                select(AssistantLifecycle)
                .where(AssistantLifecycle.id == row_id, AssistantLifecycle.finished_at.is_(None))
                .with_for_update(skip_locked=True)
            )
            if row is not None:
                row.attempts += 1
                row.error_code, row.updated_at = code, now
                row.next_attempt_at = now + backoff(row.attempts)
                await session.commit()
    except Exception as exc:  # noqa: BLE001 兜底本身出错也不能让循环停下
        log.warning("assistant_lifecycle_push_back_failed", error=type(exc).__name__)


async def _run_one(
    factory: async_sessionmaker[AsyncSession],
    cipher: Cipher,
    messenger: Messenger,
    row_id: uuid.UUID,
    now: datetime,
) -> list[Notice]:
    texts: list[str] = []
    async with factory() as session:
        row = await session.scalar(
            select(AssistantLifecycle)
            .where(
                AssistantLifecycle.id == row_id,
                AssistantLifecycle.finished_at.is_(None),
                AssistantLifecycle.next_attempt_at <= now,
            )
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return []
        target = (row.platform_app_id, row.platform_user_id)
        steps = provisioning.STEPS if row.action == "provision" else deprovisioning.STEPS
        step = steps[row.step]
        failure: tuple[str, bool] | None = None
        try:
            async with asyncio.timeout(STEP_SECONDS):
                texts = await step(session, cipher, row, messenger)
            await session.commit()
        except StepFailed as exc:
            failure = (exc.code, True)
        except StepRetry as exc:
            failure = (exc.code, False)
        except Exception as exc:  # noqa: BLE001 意外按暂时失败处理，只记类型
            failure = (type(exc).__name__, False)
        if failure is not None:
            await session.rollback()
    if failure is not None:
        log.warning("assistant_lifecycle_step_failed", step=step.__name__, code=failure[0])
        texts = await _settle_failure(factory, row_id, failure[0], permanent=failure[1], now=now)
    return [(target[0], target[1], row_id, text) for text in texts]


async def _notify(
    factory: async_sessionmaker[AsyncSession], messenger: Messenger, notices: list[Notice]
) -> None:
    for index, (app_id, user_id, row_id, text) in enumerate(notices):
        if not user_id:
            continue
        try:
            async with factory() as session:
                await messenger.text(
                    session, app_id, user_id, text, key=f"assistant-notice:{row_id}:{index}"
                )
        except Exception as exc:  # noqa: BLE001 通知发不出去不影响流程
            log.warning("assistant_lifecycle_notice_failed", error=type(exc).__name__)


async def tick(
    factory: async_sessionmaker[AsyncSession],
    cipher: Cipher,
    now: datetime,
    *,
    messenger: Messenger | None = None,
) -> dict[str, int]:
    owned = messenger is None
    messenger = messenger or Messenger(cipher)
    counts = {"assistant_lifecycle_steps": 0}
    try:
        counts["assistant_offers_expired"] = await _expire_offers(factory, now)
        counts["assistant_departures"] = await _departures(factory)
        async with factory() as session:
            due = list(
                await session.scalars(
                    select(AssistantLifecycle.id)
                    .where(
                        AssistantLifecycle.finished_at.is_(None),
                        AssistantLifecycle.next_attempt_at <= now,
                    )
                    .order_by(AssistantLifecycle.next_attempt_at)
                    .limit(BATCH)
                )
            )
        for row_id in due:
            try:
                notices = await _run_one(factory, cipher, messenger, row_id, now)
            except Exception as exc:  # noqa: BLE001 一条流程出错不挡别的
                log.warning("assistant_lifecycle_failed", error=type(exc).__name__)
                await _push_back(factory, row_id, type(exc).__name__, now)
                continue
            counts["assistant_lifecycle_steps"] += 1
            await _notify(factory, messenger, notices)
        counts.update(await retired_dirs.tick(factory, cipher, now))
    finally:
        if owned:
            await messenger.aclose()
    return counts

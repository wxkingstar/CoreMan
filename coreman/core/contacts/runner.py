"""同步任务的生命周期：contact_sync_runs 行的创建、执行、收尾。"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.contacts.feishu_source import fetch_feishu_directory
from coreman.core.contacts.sync import ContactSyncService, SyncAborted
from coreman.core.contacts.types import Directory
from coreman.core.contacts.wecom_source import fetch_wecom_directory
from coreman.core.crypto import Cipher
from coreman.core.db.models import ContactSyncRun, PlatformApp
from coreman.core.errors import ApiError, not_found
from coreman.core.logging import get_logger
from coreman.core.platforms.feishu import FeishuClient, FeishuError
from coreman.core.platforms.wecom import WeComClient, WeComError

log = get_logger(__name__)
STALE_AFTER = timedelta(minutes=30)
_IP_HINT = (
    "企微限制：新增 IP 不能读取通讯录详情(48009)，请把本机 IP 加入企业微信「通讯录同步」"
    "可信 IP，或在原白名单机器上运行"
)
Fetcher = Callable[[WeComClient], Awaitable[Directory]]


async def start_run(
    factory: async_sessionmaker[AsyncSession],
    app_id: uuid.UUID,
    triggered_by: uuid.UUID | None,
    *,
    session: AsyncSession | None = None,
) -> ContactSyncRun:
    """开一条同步记录：应用必须存在且具备企微通讯录同步能力；同应用不能并发同步。

    僵死的 running 行（超过 STALE_AFTER 未结束，多半是进程被杀）标记为 failed 后放行，
    真正仍在跑的 running 行则拒绝新请求。

    Args:
        factory: 会话工厂（session 为 None 时自开一个会话并提交）
        app_id: 平台应用 ID
        triggered_by: 触发者用户 ID（CLI 触发时为 None）
        session: 调用方会话；传入时只 flush 不提交，由调用方把 run 行与审计放进同一事务

    Returns:
        新建的 running 状态 ContactSyncRun

    Raises:
        ApiError: 应用不存在（404）、不具备同步能力（400）、已有同步在跑（409）
    """
    if session is not None:
        return await _start_run_in(session, app_id, triggered_by, commit=False)
    async with factory() as own:
        return await _start_run_in(own, app_id, triggered_by, commit=True)


async def _start_run_in(
    session: AsyncSession,
    app_id: uuid.UUID,
    triggered_by: uuid.UUID | None,
    *,
    commit: bool,
) -> ContactSyncRun:
    """start_run 的实际检查与插入；commit=False 时只 flush，事务边界交给调用方。"""
    app = await session.get(PlatformApp, app_id)
    if app is None:
        raise not_found("平台应用不存在")
    if not app.enabled or "contact_sync" not in app.capabilities:
        raise ApiError(400, 400, "该应用未启用或不具备通讯录同步能力")
    running = (
        (
            await session.execute(
                select(ContactSyncRun).where(
                    ContactSyncRun.platform_app_id == app_id,
                    ContactSyncRun.status == "running",
                )
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    for r in running:
        if now - r.started_at > STALE_AFTER:
            r.status, r.finished_at, r.error = (
                "failed",
                now,
                "执行进程消失（超过 30 分钟无结果）",
            )
        else:
            raise ApiError(409, 409, "该应用正在同步中")
    run = ContactSyncRun(
        platform_app_id=app_id, triggered_by=triggered_by, status="running", started_at=now
    )
    session.add(run)
    if commit:
        await session.commit()
        await session.refresh(run)
    else:
        await session.flush()
    return run


async def execute_run(
    factory: async_sessionmaker[AsyncSession],
    cipher: Cipher,
    run_id: int,
    fetch: Fetcher | None = None,
) -> ContactSyncRun:
    """执行一次同步：解密 secret → 抓取目录 → 归并 → 把结果写回 run 行。

    load run/app、解密 secret、构造 WeComClient、抓取、归并全部在同一个 try 里：任何一步失败
    （含 secret 解密失败、WeComClient 构造失败）都要落到 run 行上，不能让后台任务静默吞掉；
    WeComClient 一旦构造成功，无论成败都要在 finally 里关闭。

    Args:
        factory: 会话工厂
        cipher: 用于解密 platform_apps.secret_enc 的密钥
        run_id: start_run 建好的 ContactSyncRun.id
        fetch: 抓取函数，默认用 fetch_wecom_directory（测试注入假抓取）

    Returns:
        更新后的 ContactSyncRun（status 为 success/aborted/failed 之一）

    Raises:
        ValueError: run_id 对应的记录不存在——此时没有行可以落库，只能抛出
    """
    fetcher = fetch or fetch_wecom_directory
    status: str = "success"
    stats: dict[str, Any] = {}
    error: str | None = None
    client: WeComClient | FeishuClient | None = None
    try:
        async with factory() as session:
            run = await session.get(ContactSyncRun, run_id)
            if run is None:
                raise ValueError(f"同步记录 {run_id} 不存在")
            app = await session.get(PlatformApp, run.platform_app_id)
            assert app is not None
            secret = cipher.decrypt(app.secret_enc, "platform_apps.secret_enc")
            corp_id = app.corp_id or ""
        if app.platform == "feishu":
            client = FeishuClient(app.app_id or "", secret)
            directory = await fetch_feishu_directory(client)
        else:
            client = WeComClient(corp_id, secret)
            directory = await fetcher(client)
        stats = (await ContactSyncService(factory).apply(directory)).to_dict()
    except SyncAborted as exc:
        status, error = "aborted", str(exc)
    except FeishuError as exc:
        status, error = "failed", str(exc)
    except WeComError as exc:
        status, error = "failed", (_IP_HINT if exc.errcode == 48009 else str(exc))
    except Exception as exc:  # noqa: BLE001 任何异常都要落到 run 行，不能吞
        status, error = "failed", f"{type(exc).__name__}: {exc}"
        log.exception("contact_sync_failed", run_id=run_id)
    finally:
        if client is not None:
            await client.aclose()
    async with factory() as session:
        run = await session.get(ContactSyncRun, run_id)
        if run is None:
            raise ValueError(f"同步记录 {run_id} 不存在")
        run.status, run.stats, run.error, run.finished_at = (
            status,
            stats,
            error,
            datetime.now(UTC),
        )
        await session.commit()
        await session.refresh(run)
        return run

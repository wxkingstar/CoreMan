"""通讯录定时全量同步：每个启用且带 contact_sync 能力的平台应用，距上一次同步满间隔就再跑一次。

「上一次」取该应用最近一条 contact_sync_runs 的开始时间，手动同步、CLI 同步也算：刚手动同步过
的应用不会紧接着再跑；换主、重启也不会让间隔从头算或立刻补跑。开跑仍走 runner.start_run，
同应用已有同步在跑（含管理台手动触发的）时它返回 409，这一轮跳过即可。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.contacts import runner
from coreman.core.crypto import Cipher
from coreman.core.db.models import ContactSyncRun, PlatformApp
from coreman.core.errors import ApiError
from coreman.core.logging import get_logger

log = get_logger(__name__)


async def run_due(
    factory: async_sessionmaker[AsyncSession],
    cipher: Cipher,
    now: datetime,
    interval: timedelta,
    fetch: runner.Fetcher | None = None,
) -> dict[str, int]:
    """把到期的应用逐个同步一遍（同步执行，跑完才返回）。

    Args:
        factory: 会话工厂
        cipher: 解密平台应用 secret
        now: 当前时间
        interval: 同步间隔
        fetch: 企微抓取函数，透传给 runner.execute_run（测试注入）

    Returns:
        各结果的计数：contact_sync_success / aborted / failed / skipped
    """
    last = (
        select(ContactSyncRun.platform_app_id, func.max(ContactSyncRun.started_at).label("at"))
        .group_by(ContactSyncRun.platform_app_id)
        .subquery()
    )
    async with factory() as session:
        rows = (
            await session.execute(
                select(PlatformApp.id, PlatformApp.name, last.c.at)
                .outerjoin(last, last.c.platform_app_id == PlatformApp.id)
                .where(
                    PlatformApp.enabled.is_(True),
                    PlatformApp.capabilities.any("contact_sync"),  # type: ignore[arg-type]
                )
                .order_by(PlatformApp.name)
            )
        ).all()
    counts = {
        "contact_sync_success": 0,
        "contact_sync_aborted": 0,
        "contact_sync_failed": 0,
        "contact_sync_skipped": 0,
    }
    for app_id, name, at in rows:
        if at is not None and now - at < interval:
            continue
        try:
            run = await runner.start_run(factory, app_id, None)
        except ApiError as exc:
            # 409：手动同步正在跑；400/404：查询之后应用被停用或删除。都等下一轮再看。
            counts["contact_sync_skipped"] += 1
            log.info("contact_sync_scheduled_skipped", app=name, status=exc.status_code)
            continue
        done = await runner.execute_run(factory, cipher, run.id, fetch)
        counts[f"contact_sync_{done.status}"] += 1
        log.info(
            "contact_sync_scheduled",
            app=name,
            run_id=done.id,
            status=done.status,
            error=done.error,
            # 只记计数：冲突清单里是成员登录名，不进日志
            **{k: v for k, v in (done.stats or {}).items() if isinstance(v, int)},
        )
    return counts

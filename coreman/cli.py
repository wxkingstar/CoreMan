"""运维命令：python -m coreman.cli contact-sync [--app NAME]（对应 coreman contact-sync）。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from sqlalchemy import select

from coreman.api.errors import ApiError
from coreman.core.config import get_settings
from coreman.core.contacts import runner
from coreman.core.db.models import PlatformApp
from coreman.core.db.session import make_engine, make_session_factory
from coreman.core.logging import configure_logging


async def _contact_sync(app_name: str | None) -> int:
    """找到唯一一个具备通讯录同步能力的企微应用并同步；多条候选时要求 --app 指定。

    Args:
        app_name: --app 指定的平台应用名称，None 表示不限定

    Returns:
        进程退出码：同步成功为 0，候选数不为 1 或同步失败为 1
    """
    settings = get_settings()
    # 日志走 stderr：stdout 只留给下面那行结果 JSON，便于 `| jq` 直接消费
    configure_logging(
        service="cli", instance="contact-sync", level=settings.log_level, stream=sys.stderr
    )
    # 整个通讯录归并在一笔事务里、先排队等跨平台咨询锁：运维命令不套单语句超时。
    engine = make_engine(settings.database_url, command_timeout=None)
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            stmt = select(PlatformApp).where(
                PlatformApp.platform == "wecom",
                PlatformApp.enabled.is_(True),
                PlatformApp.capabilities.any("contact_sync"),  # type: ignore[arg-type]
            )
            if app_name:
                stmt = stmt.where(PlatformApp.name == app_name)
            apps = (await session.execute(stmt)).scalars().all()
        if len(apps) != 1:
            print(
                f"匹配到 {len(apps)} 个具备通讯录同步能力的企微应用，请用 --app 指定名称",
                file=sys.stderr,
            )
            return 1
        try:
            run = await runner.start_run(factory, apps[0].id, None)
            done = await runner.execute_run(factory, settings.build_cipher(), run.id)
        except ApiError as exc:
            print(exc.message, file=sys.stderr)
            return 1
        result = {
            "run_id": done.id,
            "status": done.status,
            "stats": done.stats,
            "error": done.error,
        }
        print(json.dumps(result, ensure_ascii=False))
        return 0 if done.status == "success" else 1
    finally:
        await engine.dispose()


async def _skills_sync(url: str, source: str) -> int:
    from coreman.core.knowledge.catalog_sync import fetch_catalog, sync_catalog

    settings = get_settings()
    try:
        entries = await asyncio.to_thread(fetch_catalog, url)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    engine = make_engine(settings.database_url)
    try:
        async with make_session_factory(engine)() as session:
            result = await sync_catalog(session, source_key=source, url=url, entries=entries)
            await session.commit()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：目前只有 contact-sync 一个子命令。"""
    parser = argparse.ArgumentParser(prog="coreman.cli", description="CoreMan 运维命令")
    sub = parser.add_subparsers(dest="command", required=True)
    cs = sub.add_parser("contact-sync", help="同步企业微信通讯录")
    cs.add_argument("--app", default=None, help="平台应用名称（多个应用时必填）")
    skills = sub.add_parser("skills", help="技能目录维护")
    operations = skills.add_subparsers(dest="operation", required=True)
    sync = operations.add_parser("sync", help="从已登记来源同步技能展示元数据")
    sync.add_argument("url", help="已登记的 marketplace Git 地址")
    sync.add_argument("--source", required=True, help="来源标识")
    args = parser.parse_args(argv)
    if args.command == "skills" and args.operation == "sync":
        return asyncio.run(_skills_sync(args.url, args.source))
    if args.command == "contact-sync":
        return asyncio.run(_contact_sync(args.app))
    return 2


if __name__ == "__main__":
    sys.exit(main())

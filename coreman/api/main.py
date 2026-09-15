"""coreman-api 入口：管理 API + 基础设施 API + SPA。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from coreman import __version__
from coreman.api.errors import install_error_handlers
from coreman.api.routers import (
    alert_settings,
    announcements,
    audit_logs,
    auth,
    auth_feishu,
    auth_wecom,
    bot_memories,
    bot_skills,
    bots,
    bots_extra,
    chat_logs,
    cron_jobs,
    departments,
    dev,
    escalations,
    health,
    health_report,
    infra_memories,
    infra_notify,
    infra_org,
    infra_push,
    infra_relay,
    infra_system_test,
    infrastructure_admin,
    metrics,
    model_catalog,
    notification_settings,
    objects,
    platform_apps,
    relay_agent,
    relay_servers,
    runtime,
    runtime_nodes,
    runtime_protocol,
    skill_catalog,
    statistics,
    teams,
    users,
    wecom_app_callback,
)
from coreman.api.routers import settings as settings_router
from coreman.api.spa import mount_spa
from coreman.core.bus.notify import RUNTIME_CHANNELS, Listener, asyncpg_dsn
from coreman.core.config import Settings, get_settings
from coreman.core.crypto import Cipher
from coreman.core.db.session import make_engine, make_session_factory
from coreman.core.logging import configure_logging, get_logger
from coreman.core.runtime_nodes import transport as runtime_transport
from coreman.core.settings_store import SettingsStore


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(service="api", instance=cfg.instance_name or "api", level=cfg.log_level)
        app.state.settings = cfg
        app.state.engine = make_engine(cfg.database_url)
        app.state.session_factory = make_session_factory(app.state.engine)
        app.state.settings_store = SettingsStore(app.state.session_factory)
        app.state.cipher = Cipher(cfg.master_key_bytes)
        background_tasks: set[asyncio.Task[Any]] = set()
        app.state.background_tasks = background_tasks
        # 反向通道：节点长轮询与响应帧的消费靠这条 LISTEN 连接唤醒，并共用本进程的连接池。
        app.state.runtime_listener = Listener(asyncpg_dsn(cfg.database_url), RUNTIME_CHANNELS)
        await app.state.runtime_listener.start()
        runtime_transport.configure(app.state.session_factory, app.state.runtime_listener)
        get_logger(__name__).info("api_started", version=__version__)
        try:
            yield
        finally:
            await asyncio.gather(*app.state.background_tasks, return_exceptions=True)
            runtime_transport.configure(None)
            await app.state.runtime_listener.stop()
            await app.state.engine.dispose()

    app = FastAPI(
        title="CoreMan",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    install_error_handlers(app)
    app.include_router(alert_settings.router)
    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(objects.router)
    app.include_router(infrastructure_admin.public_router)
    app.include_router(infrastructure_admin.router)
    app.include_router(infra_org.router)
    app.include_router(infra_memories.router)
    from coreman.api.routers import bot_collaboration

    app.include_router(bot_collaboration.router)
    app.include_router(escalations.router)
    app.include_router(wecom_app_callback.router)
    app.include_router(infra_notify.router)
    app.include_router(infra_push.router)
    app.include_router(infra_system_test.router)
    app.include_router(infra_relay.router)
    app.include_router(auth.public_router)
    app.include_router(auth.admin_router)
    app.include_router(auth_wecom.router)
    app.include_router(auth_feishu.router)
    app.include_router(platform_apps.router)
    app.include_router(platform_apps.runs_router)
    app.include_router(users.router)
    app.include_router(teams.router)
    app.include_router(departments.router)
    app.include_router(model_catalog.router)
    app.include_router(relay_servers.router)
    app.include_router(relay_agent.router)
    app.include_router(bots.router)
    app.include_router(bot_memories.router)
    app.include_router(skill_catalog.router)
    app.include_router(statistics.router)
    app.include_router(health_report.router)
    app.include_router(bot_skills.router)
    app.include_router(bots_extra.router)
    app.include_router(settings_router.router)
    app.include_router(audit_logs.router)
    app.include_router(chat_logs.router)
    app.include_router(cron_jobs.router)
    app.include_router(notification_settings.router)
    app.include_router(announcements.router)
    app.include_router(runtime.router)
    app.include_router(runtime_nodes.router)
    app.include_router(runtime_protocol.router)
    if cfg.coreman_env == "dev":
        # 注入接口只在开发环境存在：生产里连路由都不注册，不是靠角色挡。
        app.include_router(dev.router)
    # 必须最后挂载：spa 的 /{full_path:path} 兜底路由会遮蔽其后注册的所有 GET 路由
    mount_spa(app, Path(cfg.web_dist_dir))
    return app


def __getattr__(name: str) -> FastAPI:
    if name == "app":
        return create_app()
    raise AttributeError(name)

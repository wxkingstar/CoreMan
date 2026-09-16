from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# 建连超时：库不可达（防火墙丢包、主机宕机）时尽快失败，而不是挂在 TCP 握手上；asyncpg 默认 60 秒。
CONNECT_TIMEOUT_SECONDS = 10.0
# 单条语句的客户端上限：兜住半开连接上永远等不到回包的查询（pool_pre_ping 只在借出时探一次）。
# 业务语句都是短事务，5 分钟只拦「挂死」，不拦正常的慢查询；到点 asyncpg 会向服务端发取消。
# 迁移（alembic env.py 自建引擎）、LISTEN 与 scheduler 咨询锁（裸 asyncpg 连接）、飞书子进程
# 都不经过这里；确实要跑更久的调用方（运维 CLI）传 command_timeout=None 关掉。
COMMAND_TIMEOUT_SECONDS = 300.0


def make_engine(
    url: str,
    *,
    connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
    command_timeout: float | None = COMMAND_TIMEOUT_SECONDS,
) -> AsyncEngine:
    """有池引擎。

    Args:
        url: postgresql+asyncpg 连接串
        connect_timeout: 建立单条连接的超时秒数
        command_timeout: 单条语句的超时秒数；None 表示不限（仅限明确要跑长语句的调用方）
    """
    # hide_parameters：SQL 参数里会有 system_prompt、密文等内容，不能随异常消息进日志。
    return create_async_engine(
        url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        hide_parameters=True,
        connect_args={"timeout": connect_timeout, "command_timeout": command_timeout},
    )


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)

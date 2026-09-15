"""运行时总线数据访问层（spec §5.4、§6）。

网关 / worker / scheduler / api 只经这里的函数读写总线表，进程之间不直连。每个函数接收
`AsyncSession` 且**不 commit**：调用方把「写业务表 + 写总线表 + pg_notify」组合进同一个事务再提交，
通知才会与数据同时可见。子模块按 `from coreman.core.bus import tasks` 方式使用。
"""

from coreman.core.bus import instances, leases, notify, outbox, streams, tasks

__all__ = ["instances", "leases", "notify", "outbox", "streams", "tasks"]

"""forkserver 预加载：子进程要用的模块在这里导入一次，各机器人子进程 fork 后共享这些内存页。

飞书 SDK 一导入就加载全部开放平台接口（约 1 万个模块），每个子进程单独导入要 200MB 左右。
导入完冻结 GC：否则子进程里的回收会扫这些对象、写脏共享页，省下的内存所剩无几。
"""

import gc

import sqlalchemy.dialects.postgresql.asyncpg  # noqa: F401  建引擎时才会导入的方言
from lark_oapi.ws import client as ws_client

import coreman.runtime.gateway_feishu.child  # noqa: F401
import coreman.runtime.gateway_feishu.service  # noqa: F401  子进程入口所在模块

# SDK 在模块级建了一个事件循环，长连接跑在它上面。fork 出来的子进程会共用它的 epoll，
# 所以这里先关掉，每个子进程启动时再各建一个（`child.run_process`）。
ws_client.loop.close()
gc.freeze()

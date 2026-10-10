"""飞书子进程从预加载过的 forkserver fork：共享 SDK 内存、各用各的长连接循环、退出码照常回报。"""

import asyncio
import gc
import multiprocessing
import os
import signal
import sys
import time
import uuid
from types import SimpleNamespace

import pytest
from lark_oapi.ws import client as ws_client

from coreman.runtime.gateway_feishu import child as child_module
from coreman.runtime.gateway_feishu import service as service_module
from coreman.runtime.gateway_feishu.service import (
    FORKSERVER,
    Child,
    ForkedProcess,
    GatewayFeishuService,
)


def _exit_with(code: int) -> None:
    sys.exit(code)


def _report_preloaded() -> None:
    # 预加载冻结了 GC，并关掉了 SDK 的模块级事件循环。
    preloaded = gc.get_freeze_count() > 0 and ws_client.loop.is_closed()
    sys.exit(0 if preloaded else 1)


async def test_forked_process_reports_exit_code():
    process = multiprocessing.get_context("spawn").Process(target=_exit_with, args=(3,))
    process.start()
    forked = ForkedProcess(process)
    assert await asyncio.wait_for(forked.wait(), timeout=30) == 3
    assert forked.returncode == 3  # 进程对象已关闭，退出码仍可读


async def test_forked_process_waits_for_exit_code_after_sentinel():
    # 哨兵已可读、退出码却还没就绪：wait 要等到能回收再返回，不能断言失败。
    read_end, write_end = os.pipe()
    os.close(write_end)
    joined = []

    class LateExit:
        sentinel = read_end
        exitcode = None

        def join(self):
            joined.append(True)
            self.exitcode = 5

        def close(self):
            os.close(read_end)

    forked = ForkedProcess(LateExit())  # type: ignore[arg-type]
    assert await asyncio.wait_for(forked.wait(), timeout=5) == 5
    assert joined == [True]


async def test_forked_process_terminate():
    process = multiprocessing.get_context("spawn").Process(target=time.sleep, args=(60,))
    process.start()
    forked = ForkedProcess(process)
    assert forked.returncode is None
    forked.terminate()
    assert await asyncio.wait_for(forked.wait(), timeout=30) == -signal.SIGTERM


async def test_children_fork_from_preloaded_server():
    process = FORKSERVER.Process(target=_report_preloaded)
    await asyncio.to_thread(process.start)
    assert await asyncio.wait_for(ForkedProcess(process).wait(), timeout=60) == 0


def _sleep_child(bot_id, instance_id, generation):
    time.sleep(60)


class SlowStartProcess(multiprocessing.get_context("spawn").Process):  # type: ignore[misc]
    """`start` 拖慢半秒，模拟首次启动等 forkserver 预加载。"""

    def start(self):
        time.sleep(0.5)
        super().start()


class SlowStartContext:
    Process = SlowStartProcess


async def test_spawn_cancelled_mid_start_keeps_the_process(monkeypatch):
    # 停机取消调度时启动还在线程里跑：进程照样会起来，必须挂在 child 上，排空时才停得掉。
    monkeypatch.setattr(service_module, "FORKSERVER", SlowStartContext())
    monkeypatch.setattr(service_module, "run_child_process", _sleep_child)
    child = Child(1, "fingerprint")
    owner = SimpleNamespace(instance_id="instance")
    task = asyncio.create_task(GatewayFeishuService.spawn(owner, uuid.uuid4(), child))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.process is not None and child.process.returncode is None
    child.process.terminate()
    assert await asyncio.wait_for(child.process.wait(), timeout=30) == -signal.SIGTERM


def test_run_process_gives_each_child_its_own_sdk_loop(monkeypatch):
    preloaded = ws_client.loop
    seen = []

    async def fake_run_child(bot_id, instance_id, generation):
        seen.append(ws_client.loop)

    monkeypatch.setattr(ws_client, "loop", preloaded)
    monkeypatch.setattr(child_module, "run_child", fake_run_child)
    child_module.run_process(uuid.uuid4(), "instance", 1)
    assert seen and seen[0] is not preloaded and not seen[0].is_closed()
    seen[0].close()

"""飞书子进程的唤醒：本 bot 的通知立刻醒，别的 bot 的通知不算，没有通知就睡到兜底间隔。"""

import asyncio
import uuid

from coreman.runtime.gateway_feishu.child import wait_for_work


class FakeListener:
    def __init__(self) -> None:
        self.queues: dict[str, asyncio.Queue[dict[str, str]]] = {
            "stream_updated": asyncio.Queue(),
            "outbox_added": asyncio.Queue(),
        }

    async def wait(self, channel: str, timeout: float = 1.0) -> list[dict[str, str]]:  # noqa: ASYNC109 与 Listener.wait 同签名
        try:
            item = await asyncio.wait_for(self.queues[channel].get(), timeout)
        except TimeoutError:
            return []
        return [item]


async def test_wakes_up_for_this_bot_and_ignores_other_bots() -> None:
    bot_id = uuid.uuid4()
    listener, stop = FakeListener(), asyncio.Event()
    loop = asyncio.get_running_loop()
    listener.queues["outbox_added"].put_nowait({"bot_id": str(uuid.uuid4())})

    async def notify_later() -> None:
        await asyncio.sleep(0.2)
        listener.queues["stream_updated"].put_nowait({"bot_id": str(bot_id)})

    started = loop.time()
    notifier = asyncio.create_task(notify_later())
    await wait_for_work(listener, bot_id, stop, 5.0)  # type: ignore[arg-type]
    await notifier
    assert 0.15 <= loop.time() - started < 2.0


async def test_sleeps_until_the_poll_interval_and_returns_at_once_when_stopping() -> None:
    listener, stop = FakeListener(), asyncio.Event()
    loop = asyncio.get_running_loop()
    started = loop.time()
    await wait_for_work(listener, uuid.uuid4(), stop, 0.3)  # type: ignore[arg-type]
    assert 0.25 <= loop.time() - started < 1.5
    stop.set()
    started = loop.time()
    await wait_for_work(listener, uuid.uuid4(), stop, 5.0)  # type: ignore[arg-type]
    assert loop.time() - started < 0.5

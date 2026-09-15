"""图片 / 文件 / 图文 / 引用走完整流水线（下载提示、失败收尾、日志字段）。"""

import asyncio
import base64

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bus import streams, tasks
from coreman.core.db.models import ChatLog, OutboxItem, TaskStream
from coreman.core.db.session import make_session_factory
from coreman.core.i18n.messages import msg
from coreman.core.wecom.media import MediaFetcher
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from tests.fakes.fake_media import FakeMedia
from tests.fakes.fake_relay import FakeRelay
from tests.integration.test_chat_handler import chat_task, stream_of
from tests.integration.worker_helpers import build_ctx, seed_bot

KEY = "k" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x02" * 30
# 中文文件名走 RFC 5987：httpx 的响应头只收 ASCII，原样中文的 header 连造都造不出来。
PDF_CD = "attachment; filename*=UTF-8''%E5%AD%A3%E6%8A%A5.pdf"


async def run_with_media(engine: AsyncEngine, task, relay: FakeRelay, media: FakeMedia):  # type: ignore[no-untyped-def]
    ctx = build_ctx(
        engine,
        task,
        relay_client_factory=lambda _r: relay.client(),
        media_fetcher=MediaFetcher(media.client()),
    )
    await ChatTaskHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    return ctx


def _image_part(url: str) -> dict:  # type: ignore[type-arg]
    return {"type": "image", "ref": {"url": url, "aeskey": KEY}}


async def test_image_round_trip(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    bot, _relay, _ = await seed_bot(db_session)
    media = FakeMedia()
    url = media.add("/p1", PNG, aeskey=KEY)
    relay = FakeRelay("normal")
    t = await chat_task(db_session, bot, "", parts=[_image_part(url)])
    await run_with_media(db_engine, t, relay, media)
    body = relay.requests[0]
    content = body["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": msg("media_prompt_image")}
    assert (
        content[1]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(PNG).decode()
    )
    s = await stream_of(db_session, t.id)
    assert msg("downloading_image") in s.thinking_md and s.is_complete
    log = (await db_session.execute(select(ChatLog))).scalar_one()
    assert log.message_type == "image" and log.message_content == msg("media_prompt_image")
    assert log.status == "success" and log.file_info is None and log.quoted_content is None
    assert media.hits == ["/p1"]


async def test_plain_text_still_sends_a_string(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _relay, _ = await seed_bot(db_session)
    relay = FakeRelay("normal")
    t = await chat_task(db_session, bot, "纯文本")
    await run_with_media(db_engine, t, relay, FakeMedia())
    assert relay.requests[0]["messages"][1]["content"] == "纯文本"


async def test_download_can_be_cancelled_before_relay_starts(db_engine, db_session):
    bot, _, _ = await seed_bot(db_session)
    media = FakeMedia()
    url = media.add("/slow", PNG, aeskey=KEY, delay=30)
    relay = FakeRelay("normal")
    task = await chat_task(db_session, bot, "", parts=[_image_part(url)])
    ctx = build_ctx(
        db_engine,
        task,
        relay_client_factory=lambda _: relay.client(),
        media_fetcher=MediaFetcher(media.client()),
    )
    runner = asyncio.create_task(ChatTaskHandler().run(ctx))
    try:
        for _ in range(100):
            if media.hits:
                break
            await asyncio.sleep(0.02)
        assert media.hits
        ctx.request_cancel("user_stop")
        await asyncio.wait_for(runner, 3)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    await ctx.chat_logs.drain(5)
    assert not relay.requests
    stream = await stream_of(db_session, task.id)
    assert stream.is_complete
    log = (await db_session.execute(select(ChatLog).where(ChatLog.task_id == task.id))).scalar_one()
    assert log.status == "stopped"


async def test_file_and_quote_fill_log_fields(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _relay, _ = await seed_bot(db_session)
    media = FakeMedia()
    furl = media.add("/f", b"%PDF-1.5 hello", aeskey=KEY, content_disposition=PDF_CD)
    relay = FakeRelay("normal")
    t = await chat_task(
        db_session,
        bot,
        "",
        parts=[{"type": "file", "ref": {"url": furl, "aeskey": KEY}, "filename": None}],
    )
    await run_with_media(db_engine, t, relay, media)
    log = (await db_session.execute(select(ChatLog).where(ChatLog.task_id == t.id))).scalar_one()
    assert log.message_type == "file"
    assert log.file_info == {"filename": "季报.pdf", "size": 14, "mime": "application/pdf"}
    assert relay.requests[0]["messages"][1]["content"][1]["file_url"]["filename"] == "季报.pdf"
    t2 = await chat_task(
        db_session,
        bot,
        "这句什么意思",
        parts=[
            {"type": "text", "text": "这句什么意思"},
            {"type": "quote", "kind": "text", "text": "原话在此", "refs": []},
        ],
    )
    await run_with_media(db_engine, t2, relay, media)
    log2 = (await db_session.execute(select(ChatLog).where(ChatLog.task_id == t2.id))).scalar_one()
    assert log2.message_type == "text" and log2.quoted_content == "原话在此"
    assert relay.requests[1]["messages"][1]["content"] == msg(
        "quote_text_prefix", quoted="原话在此", text="这句什么意思"
    )


async def test_media_failure_ends_the_round_with_error(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _relay, _ = await seed_bot(db_session)
    media = FakeMedia()
    url = media.add("/gone", PNG, aeskey=KEY, status=404)
    relay = FakeRelay("normal")
    t = await chat_task(db_session, bot, "", parts=[_image_part(url)])
    await run_with_media(db_engine, t, relay, media)
    s = await stream_of(db_session, t.id)
    assert s.is_complete and s.final_text == msg(
        "media_image_failed", error=msg("media_reason_download_failed")
    )
    row = await tasks.get(db_session, t.id)
    assert row and row.status == "failed" and row.error_code == "media_failed"
    log = (await db_session.execute(select(ChatLog))).scalar_one()
    assert log.status == "error" and log.error_code == "media_failed"
    assert log.message_type == "image"
    assert relay.requests == []


async def test_video_and_empty_voice_are_answered_without_ai(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _relay, _ = await seed_bot(db_session)
    relay = FakeRelay("normal")
    t = await chat_task(
        db_session, bot, "", parts=[{"type": "video", "ref": {"url": "u", "aeskey": KEY}}]
    )
    await run_with_media(db_engine, t, relay, FakeMedia())
    assert (await stream_of(db_session, t.id)).final_text == msg("unsupported_message")
    t2 = await chat_task(
        db_session, bot, "", parts=[{"type": "audio", "ref": {}, "transcript": None}]
    )
    await run_with_media(db_engine, t2, relay, FakeMedia())
    assert (await stream_of(db_session, t2.id)).final_text == msg("voice_empty")
    logs = (await db_session.execute(select(ChatLog).order_by(ChatLog.id))).scalars().all()
    assert [(x.message_type, x.status) for x in logs] == [
        ("video", "success"),
        ("voice", "success"),
    ]
    assert relay.requests == []


async def _drain_stream(db_engine: AsyncEngine, task_id: int, runner: asyncio.Task[None]) -> None:
    """模拟网关在下载途中排空这条流：翻 proactive 并记下 finish 帧已经推过。"""
    for _ in range(200):
        async with make_session_factory(db_engine)() as s:
            if await s.get(TaskStream, task_id) is not None:
                await streams.update(s, task_id, delivery_mode="proactive")
                await streams.mark_finish_pushed(s, task_id)
                await s.commit()
                return
        if runner.done():
            runner.result()  # 处理器先跑完了：有异常就把真正的原因抛出来
            raise AssertionError("处理器已经结束，没来得及排空")
        await asyncio.sleep(0.02)
    raise AssertionError("流没有在期限内建好")


async def test_media_failure_pushes_when_the_stream_was_drained(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """下载慢到网关先排空了流：失败文案没人跟流送了，必须自己走 outbox，且不冠 ✅。"""
    bot, _relay, _ = await seed_bot(db_session)
    media = FakeMedia()
    # 1.0 秒（不是 0.3）：`_drain_stream` 以 0.02 秒轮询、上限 4 秒，要在「流建好之后、
    # 下载结束之前」这段窗口里排空。0.3 秒在负载高时不够，处理器会先跑完。
    url = media.add("/slow", PNG, aeskey=KEY, status=404, delay=1.0)
    t = await chat_task(db_session, bot, "", parts=[_image_part(url)])
    tid = t.id
    ctx = build_ctx(
        db_engine,
        t,
        relay_client_factory=lambda _r: FakeRelay("normal").client(),
        media_fetcher=MediaFetcher(media.client()),
    )
    runner = asyncio.create_task(ChatTaskHandler().run(ctx))
    try:
        await _drain_stream(db_engine, tid, runner)
        await asyncio.wait_for(runner, 10)
    finally:
        runner.cancel()
    await ctx.chat_logs.drain(5)
    db_session.expire_all()
    failed = msg("media_image_failed", error=msg("media_reason_download_failed"))
    item = (await db_session.execute(select(OutboxItem))).scalar_one()
    assert item.dedupe_key == f"{tid}:send:final" and item.target == {"chat_id": "zs"}
    assert failed in item.payload["markdown"]
    assert not item.payload["markdown"].startswith(msg("bg_done_prefix"))
    assert (await tasks.get(db_session, tid)).error_code == "media_failed"  # type: ignore[union-attr]


async def test_feishu_uses_its_resource_fetcher_with_shared_wecom_fetcher(
    db_engine, db_session, monkeypatch
):
    from coreman.core.bots.secrets import CREDENTIALS_AAD, encrypt_json
    from coreman.core.platforms.feishu_media import FeishuMediaFetcher
    from coreman.core.wecom.media import Media

    bot, _, cipher = await seed_bot(db_session)
    bot.platform = "feishu"
    bot.credentials_enc = encrypt_json(
        cipher, {"app_id": "cli_test", "app_secret": "synthetic"}, CREDENTIALS_AAD
    )
    await db_session.commit()
    calls = []

    async def fetch(self, ref):
        calls.append(ref)
        return Media(PNG, "image/png", None)

    monkeypatch.setattr(FeishuMediaFetcher, "fetch_image", fetch)
    relay = FakeRelay("normal")
    task = await chat_task(
        db_session,
        bot,
        "",
        parts=[{"type": "image", "ref": {"file_key": "img_test", "message_id": "om_in"}}],
    )
    await run_with_media(db_engine, task, relay, FakeMedia())
    assert calls == [{"file_key": "img_test", "message_id": "om_in"}]
    assert relay.requests[0]["messages"][1]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;"
    )

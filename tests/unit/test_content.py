import asyncio
import base64
import json

import pytest

from coreman.core.chat.content import BuiltContent, ContentBuilder
from coreman.core.i18n.messages import msg
from coreman.core.wecom.media import MediaFetcher
from tests.fakes.fake_media import FakeMedia

KEY = "ab" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 20


def _text(t: str) -> dict:  # type: ignore[type-arg]
    return {"type": "text", "text": t}


def _image(url: str | None, aeskey: str | None = KEY) -> dict:  # type: ignore[type-arg]
    ref = {}
    if url:
        ref["url"] = url
    if aeskey:
        ref["aeskey"] = aeskey
    return {"type": "image", "ref": ref}


def _file(url: str, filename: str | None = None) -> dict:  # type: ignore[type-arg]
    return {"type": "file", "ref": {"url": url, "aeskey": KEY}, "filename": filename}


def _quote(kind: str, text: str | None = None, refs=None) -> dict:  # type: ignore[no-untyped-def, type-arg]
    return {"type": "quote", "kind": kind, "text": text, "refs": refs or []}


@pytest.fixture
def fake() -> FakeMedia:
    return FakeMedia()


@pytest.fixture
def hints() -> list[str]:
    return []


@pytest.fixture
def builder(fake: FakeMedia, hints: list[str]) -> ContentBuilder:
    return ContentBuilder(MediaFetcher(fake.client()), on_hint=hints.append)


def _data_uri(mime: str, data: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


async def test_plain_text_and_voice(builder: ContentBuilder) -> None:
    built = await builder.build([_text("你好")], text="你好")
    assert built == BuiltContent([_text("你好")], "你好", "text", None, None, None)
    voice = await builder.build(
        [{"type": "audio", "ref": {}, "transcript": "语音文本"}], text="语音文本"
    )
    assert voice.message_type == "voice" and voice.parts == [_text("语音文本")]
    empty = await builder.build([{"type": "audio", "ref": {}, "transcript": None}], text="")
    assert empty.failed == msg("voice_empty")


async def test_image_message(builder: ContentBuilder, fake: FakeMedia, hints: list[str]) -> None:
    url = fake.add("/i", PNG, aeskey=KEY)
    built = await builder.build([_image(url)], text="")
    assert built.message_type == "image" and built.failed is None
    assert built.parts == [
        _text(msg("media_prompt_image")),
        {"type": "image_url", "image_url": {"url": _data_uri("image/png", PNG)}},
    ]
    assert built.text == msg("media_prompt_image") and hints == [msg("downloading_image")]
    missing = await builder.build([_image(None)], text="")
    assert missing.parts == [_text(msg("media_image_missing"))] and missing.message_type == "image"
    gone = fake.add("/gone", PNG, aeskey=KEY, status=404)
    failed = await builder.build([_image(gone)], text="")
    assert failed.failed == msg("media_image_failed", error=msg("media_reason_download_failed"))


async def test_file_message(builder: ContentBuilder, fake: FakeMedia, hints: list[str]) -> None:
    url = fake.add(
        "/f", b"%PDF-1.7 x", aeskey=KEY, content_disposition='attachment; filename="r.pdf"'
    )
    built = await builder.build([_file(url)], text="")
    assert built.message_type == "file"
    assert built.parts == [
        _text(msg("media_prompt_file", name="r.pdf")),
        {
            "type": "file_url",
            "file_url": {
                "url": _data_uri("application/pdf", b"%PDF-1.7 x"),
                "filename": "r.pdf",
            },
        },
    ]
    assert built.file_info == {"filename": "r.pdf", "size": 10, "mime": "application/pdf"}
    assert hints == [msg("downloading_file")]
    named = await builder.build([_file(url, "季报.pdf")], text="")
    assert named.file_info and named.file_info["filename"] == "季报.pdf"
    missing = await builder.build([{"type": "file", "ref": {}, "filename": None}], text="")
    assert missing.parts == [_text(msg("media_file_missing", name=msg("unknown_filename")))]


async def test_mixed_message(builder: ContentBuilder, fake: FakeMedia, hints: list[str]) -> None:
    ok = fake.add("/ok", PNG, aeskey=KEY)
    bad = fake.add("/bad", PNG, aeskey=KEY, status=500)
    built = await builder.build(
        [_text("看图"), _image(ok), _image(bad), _image(None, None)], text="看图"
    )
    assert built.message_type == "mixed" and hints == [msg("processing_mixed")]
    assert built.parts == [
        _text("看图"),
        {"type": "image_url", "image_url": {"url": _data_uri("image/png", PNG)}},
        _text(msg("media_image_failed_item")),
        _text(msg("media_image_placeholder")),
    ]
    assert built.text == "看图\n" + msg("media_image_failed_item") + "\n" + msg(
        "media_image_placeholder"
    )
    only_images = await builder.build([_image(ok)], text="")
    assert only_images.message_type == "image"  # 单张图不是 mixed
    two_images = await builder.build([_image(ok), _image(ok)], text="")
    assert two_images.parts[0] == _text(msg("media_prompt_images"))
    assert two_images.message_type == "mixed"
    nothing = await builder.build([], text="")
    assert nothing.parts == [_text(msg("media_mixed_empty"))]


async def test_video_and_unknown_unsupported(builder: ContentBuilder) -> None:
    built = await builder.build([{"type": "video", "ref": {"url": "u", "aeskey": KEY}}], text="")
    assert built.failed == msg("unsupported_message") and built.message_type == "video"


async def test_quotes(builder: ContentBuilder, fake: FakeMedia, hints: list[str]) -> None:
    t = await builder.build([_text("回复"), _quote("text", "原话")], text="回复")
    assert t.parts == [_text(msg("quote_text_prefix", quoted="原话", text="回复"))]
    assert t.message_type == "text" and t.quoted_content == "原话"
    v = await builder.build([_text("嗯"), _quote("voice", "语音原文")], text="嗯")
    assert v.parts == [_text(msg("quote_voice_prefix", quoted="语音原文", text="嗯"))]
    url = fake.add("/qi", PNG, aeskey=KEY)
    qi = await builder.build(
        [_text("这是啥"), _quote("image", refs=[{"url": url, "aeskey": KEY}])], text="这是啥"
    )
    assert qi.message_type == "quote_image" and hints[-1] == msg("downloading_quote_image")
    assert qi.parts == [
        _text(msg("quote_image_prefix", text="这是啥")),
        {"type": "image_url", "image_url": {"url": _data_uri("image/png", PNG)}},
    ]
    nokey = await builder.build(
        [_text("这是啥"), _quote("image", refs=[{"url": url}])], text="这是啥"
    )
    assert nokey.parts == [_text(msg("quote_image_prefix", text="这是啥"))] and nokey.failed is None
    furl = fake.add("/qf", b"%PDF-1.4", aeskey=KEY)
    qf = await builder.build(
        [_text("总结"), _quote("file", refs=[{"url": furl, "aeskey": KEY}])], text="总结"
    )
    assert qf.message_type == "quote_file"
    assert qf.file_info == {"filename": "file.pdf", "size": 8, "mime": "application/pdf"}
    assert qf.parts[0] == _text(msg("quote_file_prefix", name="file.pdf", text="总结"))
    mixed = _quote(
        "mixed",
        refs=[
            {
                "msg_item": [
                    {"msgtype": "text", "text": {"content": "原文"}},
                    {"msgtype": "image", "image": {"url": url, "aeskey": KEY}},
                ]
            }
        ],
    )
    qm = await builder.build([_text("看"), mixed], text="看")
    assert qm.message_type == "quote_mixed" and qm.quoted_content == "原文 [图片]"
    assert qm.parts == [
        _text(msg("quote_text_prefix", quoted="原文 [图片]", text="看")),
        {"type": "image_url", "image_url": {"url": _data_uri("image/png", PNG)}},
    ]
    noimg = _quote(
        "mixed", refs=[{"msg_item": [{"msgtype": "text", "text": {"content": "只文字"}}]}]
    )
    qn = await builder.build([_text("看"), noimg], text="看")
    assert qn.parts == [_text(msg("quote_text_prefix", quoted="只文字", text="看"))]
    assert json.dumps(qn.parts)


async def test_on_hint_may_be_async_and_follows_locale(fake: FakeMedia) -> None:
    # 对话流水线传的是「把思考区刷给用户」的异步回调：提示必须在下载期间就 await 出去。
    seen: list[str] = []

    async def flush(line: str) -> None:
        await asyncio.sleep(0)
        seen.append(line)

    url = fake.add("/a", PNG, aeskey=KEY)
    builder = ContentBuilder(MediaFetcher(fake.client()), locale="ja", on_hint=flush)
    built = await builder.build([_image(url)], text="")
    assert built.failed is None
    assert seen == [msg("downloading_image", "ja")]
    # 提示按 locale 走，发给模型的提示词仍是中文原文（ja 表里同文）。
    assert built.parts[0] == _text(msg("media_prompt_image"))


async def test_no_hint_callback_is_fine(fake: FakeMedia) -> None:
    url = fake.add("/b", PNG, aeskey=KEY)
    built = await ContentBuilder(MediaFetcher(fake.client())).build([_image(url)], text="")
    assert built.message_type == "image" and built.failed is None


async def test_mixed_files_preserve_all_attachment_metadata(builder, fake):
    first = fake.add("/one", b"%PDF-1.7 one", aeskey=KEY)
    second = fake.add("/two", b"%PDF-1.7 second", aeskey=KEY)
    built = await builder.build(
        [_text("比较两个文件"), _file(first, "one.pdf"), _file(second, "two.pdf")],
        text="比较两个文件",
    )
    assert built.failed is None
    assert [f["filename"] for f in built.file_info["files"]] == ["one.pdf", "two.pdf"]
    assert len([p for p in built.parts if p["type"] == "file_url"]) == 2

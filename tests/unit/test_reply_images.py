import base64
import json
from types import SimpleNamespace
from typing import Any

import httpx

from coreman.core.platforms.feishu import FeishuClient
from coreman.core.prompting.rich_cards import REPLY_IMAGES_PROMPT, with_reply_images
from coreman.core.relay.sse import ImageDelta, SseParser
from coreman.runtime.gateway_feishu.cards import post_content
from coreman.runtime.worker.chat.reply_images import ReplyImages

PNG = b"\x89PNG\r\n\x1a\n-synthetic"


def _images(platform: str) -> ReplyImages:
    """只读平台；测试直接塞客户端，不解密凭据。"""
    bot: Any = SimpleNamespace(platform=platform)
    return ReplyImages(bot, None)  # type: ignore[arg-type]


def _line(images: list) -> str:
    body = {
        "id": "c",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": "", "images": images},
                "finish_reason": None,
            }
        ],
    }
    return "data: " + json.dumps(body)


def test_parser_decodes_images_and_drops_malformed_ones() -> None:
    good = {
        "name": "a.png",
        "mime_type": "image/png",
        "alt": "走势",
        "data": base64.b64encode(PNG).decode(),
    }
    bad = [
        {**good, "data": "%%%"},
        {**good, "mime_type": "text/html"},
        {**good, "data": ""},
        "not an object",
    ]
    events = SseParser("codex").feed_line(_line([good, *bad]))
    assert events == [ImageDelta("a.png", "image/png", "走势", PNG)]


def _feishu(handler) -> FeishuClient:
    def route(request: httpx.Request) -> httpx.Response:
        if "access_token" in request.url.path:
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "synthetic", "expire": 7200}
            )
        return handler(request)

    http = httpx.AsyncClient(
        base_url="https://open.feishu.cn", transport=httpx.MockTransport(route)
    )
    return FeishuClient("cli", "secret", http=http)


async def test_feishu_image_is_uploaded_and_becomes_its_own_paragraph() -> None:
    seen: list[bytes] = []

    def upload(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/open-apis/im/v1/images"
        seen.append(request.content)
        return httpx.Response(200, json={"code": 0, "data": {"image_key": "img_v3_abc"}})

    images = _images("feishu")
    images._client = _feishu(upload)
    image = ImageDelta("a.png", "image/png", "头像 [新]", PNG)
    try:
        assert await images.markdown(image, "看图：") == "\n\n![头像  新](img_v3_abc)\n\n"
        assert await images.markdown(image, "") == "![头像  新](img_v3_abc)\n\n"
    finally:
        await images.aclose()
    assert seen and b'name="image_type"' in seen[0] and PNG in seen[0]
    # 网关的富文本降级也认这种 img_key 图片。
    rows = post_content("看图\n\n![头像](img_v3_abc)\n\n完")["zh_cn"]["content"]
    assert {"tag": "img", "image_key": "img_v3_abc"} in [row[0] for row in rows]


async def test_upload_failure_and_other_platforms_leave_a_note() -> None:
    def broken(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"image_key": "../evil"}})

    images = _images("feishu")
    images._client = _feishu(broken)
    try:
        note = await images.markdown(ImageDelta("a.png", "image/png", "", PNG), "")
    finally:
        await images.aclose()
    assert note == "🖼️ a.png（图片上传失败）\n\n"
    wecom = _images("wecom")
    note = await wecom.markdown(ImageDelta("a.png", "image/png", "图", PNG), "前文")
    assert note == "\n\n🖼️ 图（当前渠道暂不支持在回复里显示图片）\n\n"


def test_reply_images_prompt_only_for_feishu_codex() -> None:
    assert with_reply_images("", "feishu", "codex") == REPLY_IMAGES_PROMPT
    assert with_reply_images("前文", "feishu", "codex") == "前文\n\n" + REPLY_IMAGES_PROMPT
    assert with_reply_images("前文", "feishu", "claude") == "前文"
    assert with_reply_images("前文", "wecom", "codex") == "前文"

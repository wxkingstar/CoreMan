import httpx
import pytest

from coreman.core.platforms.feishu import FeishuError
from coreman.runtime.gateway_feishu import images
from coreman.runtime.gateway_feishu.images import RemoteImages

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class Uploads:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    async def __call__(self, name, data, mime):
        self.calls.append((name, data, mime))
        if self.failure:
            raise FeishuError(self.failure)
        return f"img_v3_{len(self.calls)}"


def serve(routes):
    fetched = []

    def handle(request):
        fetched.append(str(request.url))
        status, body = routes.get(request.url.path, (404, b""))
        return httpx.Response(status, content=body)

    return httpx.MockTransport(handle), fetched


async def test_remote_image_becomes_image_key_and_keeps_download_link():
    transport, fetched = serve({"/a.png": (200, PNG)})
    upload = Uploads()
    remote = RemoteImages(upload, transport=transport)
    url = "https://oss.example/a.png?Signature=x&Expires=1"
    text = f"头像：\n![头像]({url})\n完成"
    result = await remote.localize(text)
    assert result == f"头像：\n![头像](img_v3_1)\n[查看原图]({url})\n完成"
    assert upload.calls == [("image.png", PNG, "image/png")]
    # 正文已经给了下载链接就不重复；同一地址只下载上传一次。
    linked = await remote.localize(f"![头像]({url})\n\n[下载头像原图]({url})（有效期 7 天）")
    assert linked == f"![头像](img_v3_1)\n\n[下载头像原图]({url})（有效期 7 天）"
    assert len(fetched) == 1 and len(upload.calls) == 1


@pytest.mark.parametrize(
    ("routes", "failure"),
    [
        ({"/a.png": (200, b"<html>not an image</html>")}, None),
        ({"/a.png": (403, b"")}, None),
        ({"/a.png": (200, PNG)}, 234001),
    ],
)
async def test_unusable_image_stays_remote_and_is_not_retried_immediately(routes, failure):
    transport, fetched = serve(routes)
    upload = Uploads(failure)
    remote = RemoteImages(upload, transport=transport)
    text = "![x](https://oss.example/a.png)"
    assert await remote.localize(text) == text
    assert await remote.localize(text) == text
    assert len(fetched) == 1


async def test_oversized_image_is_rejected_while_streaming(monkeypatch):
    monkeypatch.setattr(images, "MAX_BYTES", 16)
    transport, _ = serve({"/a.png": (200, PNG)})
    upload = Uploads()
    remote = RemoteImages(upload, transport=transport)
    assert await remote.key_for("https://oss.example/a.png") is None
    assert not upload.calls


async def test_image_count_per_message_is_bounded(monkeypatch):
    monkeypatch.setattr(images, "PER_MESSAGE", 2)
    transport, fetched = serve({f"/{i}.png": (200, PNG) for i in range(4)})
    remote = RemoteImages(Uploads(), transport=transport)
    text = "\n".join(f"![{i}](https://oss.example/{i}.png)" for i in range(4))
    result = await remote.localize(text)
    assert result.count("img_v3_") == 2 and "![3](https://oss.example/3.png)" in result
    assert len(fetched) == 2

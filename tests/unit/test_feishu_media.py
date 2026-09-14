import httpx
import pytest

from coreman.core.chat.content import ContentBuilder
from coreman.core.platforms.feishu import FeishuClient
from coreman.core.platforms.feishu_media import FeishuMediaFetcher
from coreman.core.wecom.media import MediaError


async def test_feishu_resources_are_scoped_to_current_message_and_bounded():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if "access_token" in request.url.path:
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "synthetic", "expire": 7200}
            )
        assert request.url.host == "open.feishu.cn"
        assert request.headers["Authorization"] == "Bearer synthetic"
        assert request.url.params["type"] == "file"
        return httpx.Response(200, content=b"hello", headers={"content-type": "text/plain"})

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn", transport=httpx.MockTransport(handler)
    ) as http:
        fetcher = FeishuMediaFetcher(FeishuClient("cli", "secret", http=http), message_id="om_1")
        ref = {"message_id": "om_1", "file_key": "file_x", "url": "http://169.254.169.254"}
        built = await ContentBuilder(fetcher).build(
            [{"type": "file", "ref": ref, "filename": "a.txt"}], text=""
        )
        assert built.failed is None and built.file_info["size"] == 5
        assert calls[-1] == "/open-apis/im/v1/messages/om_1/resources/file_x"
        count = len(calls)
        with pytest.raises(MediaError):
            await fetcher.fetch_file({**ref, "message_id": "other"}, filename=None)
        with pytest.raises(MediaError):
            await fetcher.fetch_file({**ref, "file_key": "../secrets"}, filename=None)
        assert len(calls) == count
        fetcher.max_bytes = 3
        with pytest.raises(MediaError, match="too_large"):
            await fetcher.fetch_file(ref, filename=None)

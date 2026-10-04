"""回复里的图片：上传到飞书换 img_key，正文写成独占一段的 `![说明](img_key)`。

图片字节只在 Runtime 节点上，随 SSE 流到 worker，网关读不到，所以在 worker 这一侧上传；
网关渲染卡片时把这种 img_key 图片直接换成原生图片组件。其他平台暂不支持回复图片，只留一行说明。
"""

from __future__ import annotations

import asyncio
import re

from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot
from coreman.core.logging import get_logger
from coreman.core.platforms.feishu import FeishuClient, FeishuError
from coreman.core.relay.sse import ImageDelta, paragraph_gap

log = get_logger(__name__)

# 飞书上传图片接口上限 10 MB。
MAX_BYTES = 10 * 1024 * 1024
UPLOAD_SECONDS = 30.0
_KEY = re.compile(r"img_[A-Za-z0-9_-]{1,240}")
_UNSAFE_ALT = re.compile(r"[\[\]\n\r]")


class ReplyImages:
    """一轮对话一份；飞书客户端用到时才建，`aclose` 收尾。"""

    def __init__(self, bot: Bot, cipher: Cipher) -> None:
        self.bot, self.cipher = bot, cipher
        self._client: FeishuClient | None = None

    async def markdown(self, image: ImageDelta, before: str) -> str:
        """接在 `before` 后面的一段：图片前后各空一行，网关才会把它当成独立的图片组件。"""
        label = _UNSAFE_ALT.sub(" ", image.alt).strip()
        if self.bot.platform != "feishu":
            body = f"🖼️ {label or image.name}（当前渠道暂不支持在回复里显示图片）"
        else:
            try:
                key = await asyncio.wait_for(self._upload(image), UPLOAD_SECONDS)
                body = f"![{label}]({key})"
            except (FeishuError, TimeoutError) as exc:
                log.warning("reply_image_upload_failed", error=type(exc).__name__)
                body = f"🖼️ {label or image.name}（图片上传失败）"
        return paragraph_gap(before, body) + body + "\n\n"

    async def _upload(self, image: ImageDelta) -> str:
        if len(image.data) > MAX_BYTES:
            raise FeishuError(-2, "image too large")
        if self._client is None:
            creds = decrypt_json(self.cipher, self.bot.credentials_enc, CREDENTIALS_AAD)
            self._client = FeishuClient(creds.get("app_id", ""), creds.get("app_secret", ""))
        result = await self._client.call(
            "POST",
            "/open-apis/im/v1/images",
            data={"image_type": "message"},
            files={"image": (image.name, image.data, image.mime_type)},
        )
        key = (result.get("data") or {}).get("image_key")
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise FeishuError(-2, "invalid image key")
        return key

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

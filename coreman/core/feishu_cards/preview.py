"""真机预览：把样例或指定的回复编译成卡片，用某个飞书 AI 员工发给指定成员。

用于视觉回归（浅色 / 深色主题、PC / 手机）。默认只调 CardKit 建卡片实体做校验，不发消息；
加 `apply=True` 才真正发送。`stream=True` 时第一张卡走和线上一样的流程：先流式打字，
再关流式、整卡换成富卡片。
"""

from __future__ import annotations

import asyncio
import json
import math
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, User, UserIdentity
from coreman.core.feishu_cards.compile import compile_reply, image_urls
from coreman.core.feishu_cards.samples import SAMPLE_IMAGE_URL
from coreman.core.feishu_cards.thinking import thinking_panel
from coreman.core.platforms.feishu import BASE_URL, FeishuClient

Echo = Callable[[str], None]


@dataclass(frozen=True)
class Target:
    bot: Bot
    receive_id: str
    name: str


class PreviewError(RuntimeError):
    """找不到机器人或接收人；消息直接给命令行用户看。"""


async def resolve(factory: async_sessionmaker[Any], *, bot_key: str | None, to: str) -> Target:
    """按 bot_key 找启用中的飞书 AI 员工，按登录名或姓名找有飞书身份的成员。"""
    async with factory() as session:
        bots = list(
            await session.scalars(
                select(Bot).where(Bot.platform == "feishu", Bot.enabled.is_(True))
            )
        )
        rows = (
            await session.execute(
                select(User.display_name, UserIdentity.platform_user_id)
                .join(UserIdentity, UserIdentity.user_id == User.id)
                .where(
                    UserIdentity.platform == "feishu",
                    User.status == "active",
                    (User.login_name == to) | (User.display_name == to),
                )
            )
        ).all()
    chosen = [b for b in bots if bot_key in {b.bot_key, None}]
    if len(chosen) != 1:
        keys = "、".join(b.bot_key for b in bots) or "无"
        raise PreviewError(f"请用 --bot 指定一个启用中的飞书 AI 员工（现有：{keys}）")
    if len(rows) != 1:
        raise PreviewError(f"--to 匹配到 {len(rows)} 个有飞书身份的成员，请换成登录名")
    return Target(chosen[0], str(rows[0][1]), str(rows[0][0]))


class _Api:
    """直接调飞书接口：预览要把飞书返回的错误说明原样打出来，便于排查卡片字段。"""

    def __init__(self, token: str, echo: Echo) -> None:
        self.http = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {token}"},
            trust_env=False,
            timeout=20,
        )
        self.echo = echo

    async def call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any] | None:
        body: dict[str, Any] = (await self.http.request(method, path, **kwargs)).json()
        if body.get("code") == 0:
            data = body.get("data")
            return data if isinstance(data, dict) else {}
        detail = json.dumps(body.get("error") or {}, ensure_ascii=False)[:800]
        self.echo(f"  ✗ {path}: code={body.get('code')} msg={body.get('msg')} {detail}")
        return None

    async def create(self, card: dict[str, Any]) -> str | None:
        data = await self.call(
            "POST",
            "/open-apis/cardkit/v1/cards",
            json={"type": "card_json", "data": json.dumps(card, ensure_ascii=False)},
        )
        return str(data["card_id"]) if data and data.get("card_id") else None

    async def send(self, receive_id: str, card_id: str) -> bool:
        data = await self.call(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": "user_id"},
            json={
                "receive_id": receive_id,
                "msg_type": "interactive",
                "content": json.dumps({"type": "card", "data": {"card_id": card_id}}),
            },
        )
        return data is not None

    async def upload(self, name: str, data: bytes) -> str | None:
        body = await self.call(
            "POST",
            "/open-apis/im/v1/images",
            data={"image_type": "message"},
            files={"image": (name, data, "image/png")},
        )
        return str(body["image_key"]) if body and body.get("image_key") else None


async def run(
    target: Target,
    cipher: Cipher,
    texts: dict[str, str],
    *,
    apply: bool,
    stream: bool,
    echo: Echo = print,
) -> bool:
    """逐条编译、校验，`apply` 时发送。返回是否全部成功。"""
    creds = decrypt_json(cipher, target.bot.credentials_enc, CREDENTIALS_AAD)
    client = FeishuClient(creds["app_id"], creds["app_secret"])
    try:
        token = await client.get_token()
    finally:
        await client.aclose()
    api = _Api(token, echo)
    ok = True
    try:
        echo(f"AI 员工：{target.bot.name}（{target.bot.bot_key}）；接收人：{target.name}")
        images: dict[str, str] = {}
        if any(SAMPLE_IMAGE_URL in image_urls(t) for t in texts.values()):
            key = await api.upload("sample.png", sample_image_png())
            if key:
                images[SAMPLE_IMAGE_URL] = key
        prefix = [thinking_panel("🔎 查询数据\n📊 汇总分析\n✍️ 整理结论")]
        for index, (name, text) in enumerate(texts.items()):
            compiled = compile_reply(text, prefix=prefix, images=images)
            sizes = [len(json.dumps(c, ensure_ascii=False).encode()) for c in compiled.cards]
            echo(f"[{name}] {len(compiled.cards)} 张卡，{sizes} 字节")
            for problem in compiled.problems:
                echo(f"  ! 自检：{problem}")
            ok = ok and not compiled.problems
            if stream and index == 0 and apply:
                ok = await _stream(api, target.receive_id, text, compiled.cards[0]) and ok
                rest = compiled.cards[1:]
            else:
                rest = compiled.cards
            for card in rest:
                card_id = await api.create(card)
                if card_id is None:
                    ok = False
                    continue
                echo("  ✓ 飞书校验通过")
                if apply:
                    ok = await api.send(target.receive_id, card_id) and ok
                    await asyncio.sleep(0.4)
        if not apply:
            echo("只做了校验，没有发送。确认无误后加 --apply。")
        return ok
    finally:
        await api.http.aclose()


async def _stream(api: _Api, receive_id: str, text: str, final: dict[str, Any]) -> bool:
    """和线上一样：先流式打字、块写完即插入，结束时关流式、写摘要、整卡换成富卡片。"""
    from coreman.core.feishu_cards.compile import card_shell
    from coreman.core.feishu_cards.stream import plan, stream_units

    elements: list[dict[str, Any]] = []
    panel = final["body"]["elements"][0] if final["body"]["elements"] else None
    if panel and panel.get("tag") == "collapsible_panel":
        elements.append(panel)
    elements.append({"tag": "markdown", "element_id": "answer", "content": "…"})
    streaming = card_shell(elements, streaming=True, streaming_config=STREAMING)
    card_id = await api.create(streaming)
    if card_id is None or not await api.send(receive_id, card_id):
        return False
    layout: list[dict[str, Any]] = [{"kind": "text", "ids": ["answer"], "text": "…"}]
    seq = 0
    step = max(8, len(text) // 40)
    for end in [*range(step, len(text), step), len(text)]:
        steps = plan(layout, stream_units(text[:end]), anchor="thinking_panel", shown=3_000)
        if steps.actions:
            seq += 1
            await api.call(
                "POST",
                f"/open-apis/cardkit/v1/cards/{card_id}/batch_update",
                json={"sequence": seq, "actions": json.dumps(steps.actions, ensure_ascii=False)},
            )
        for element_id, content in steps.texts:
            seq += 1
            await api.call(
                "PUT",
                f"/open-apis/cardkit/v1/cards/{card_id}/elements/{element_id}/content",
                json={"sequence": seq, "content": content},
            )
        layout = steps.layout
        await asyncio.sleep(0.35)
    config: dict[str, Any] = {"streaming_mode": False}
    summary = (final.get("config") or {}).get("summary")
    if summary:
        config["summary"] = summary
    seq += 1
    await api.call(
        "PATCH",
        f"/open-apis/cardkit/v1/cards/{card_id}/settings",
        json={"sequence": seq, "settings": json.dumps({"config": config}, ensure_ascii=False)},
    )
    seq += 1
    done = await api.call(
        "PUT",
        f"/open-apis/cardkit/v1/cards/{card_id}",
        json={
            "sequence": seq,
            "card": {"type": "card_json", "data": json.dumps(final, ensure_ascii=False)},
        },
    )
    if done is not None:
        api.echo("  ✓ 已流式发送（块写完即插入），并整卡替换为富卡片")
    return done is not None


# 与网关一致的打字参数（gateway_feishu/cards.py 的 STREAMING_CONFIG）。
STREAMING: dict[str, Any] = {
    "print_frequency_ms": {"default": 40},
    "print_step": {"default": 2},
    "print_strategy": "fast",
}


def sample_image_png(size: int = 240) -> bytes:
    """样例商品图：纯标准库画一只手袋（PNG），免得预览依赖外部图片地址。"""
    bg, body, gold = (240, 236, 229), (28, 28, 30), (201, 162, 90)
    scale = size / 240

    def pixel(x: float, y: float) -> tuple[int, int, int]:
        x, y = x / scale, y / scale
        top, bottom = 92, 196
        if top <= y <= bottom:
            half = 62 + 12 * (y - top) / (bottom - top)
            if abs(x - 120) <= half:
                if y > bottom - 14 and abs(x - 120) > half - 14:
                    cx = 120 + math.copysign(half - 14, x - 120)
                    if math.hypot(x - cx, y - (bottom - 14)) > 14:
                        return bg
                if 108 <= x <= 132 and 118 <= y <= 146 or 100 <= x <= 140 and 92 <= y <= 102:
                    return gold
                return body
        if y < 96 and 40 <= math.hypot(x - 120, y - 96) <= 48:
            return body
        return bg

    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            rows.extend(pixel(x + 0.5, y + 0.5))

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )

"""Name only known groups, using this bot's existing credentials and bounded cache."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict
from urllib.parse import quote

from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot
from coreman.core.platforms.feishu import FeishuClient, FeishuError

_CACHE: OrderedDict[tuple[str, str, str], tuple[float, str]] = OrderedDict()
TTL_SECONDS = 300
MAX_LOOKUPS = 20


async def known_chat_options(bot: Bot, chat_ids: list[str], cipher: Cipher) -> list[dict[str, str]]:
    ids = list(dict.fromkeys(chat_ids))[:200]
    names = dict.fromkeys(ids, "")
    if bot.platform != "feishu":
        return [{"id": cid, "name": cid} for cid in ids]
    scope = (str(bot.id), hashlib.sha256(bot.credentials_enc.encode()).hexdigest())
    now = time.monotonic()
    missing = []
    for cid in ids:
        cached = _CACHE.get((*scope, cid))
        if cached and cached[0] > now:
            names[cid] = cached[1]
        else:
            missing.append(cid)
    if missing:
        try:
            config = decrypt_json(cipher, bot.credentials_enc, CREDENTIALS_AAD)
            client = FeishuClient(config["app_id"], config["app_secret"])
        except (ValueError, KeyError, TypeError):
            return [{"id": cid, "name": names[cid] or cid} for cid in ids]
        slots = asyncio.Semaphore(4)

        async def resolve(cid: str) -> None:
            name = cid
            try:
                async with slots:
                    body = await client.call("GET", f"/open-apis/im/v1/chats/{quote(cid, safe='')}")
                    value = (body.get("data") or {}).get("name")
                    if isinstance(value, str) and value.strip():
                        name = value[:200]
            except (FeishuError, ValueError, TypeError):
                pass
            names[cid] = name
            _CACHE[(*scope, cid)] = (time.monotonic() + TTL_SECONDS, name)
            _CACHE.move_to_end((*scope, cid))
            while len(_CACHE) > 2048:
                _CACHE.popitem(last=False)

        try:
            async with asyncio.timeout(5):
                await asyncio.gather(*(resolve(cid) for cid in missing[:MAX_LOOKUPS]))
        except TimeoutError:
            pass
        finally:
            await client.aclose()
    return [{"id": cid, "name": names[cid] or cid} for cid in ids]

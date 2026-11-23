"""消息流卡片跳转：只 302 到飞书 AppLink 的打开会话 / 打开机器人，参数不合格式回 400；
带签名时顺带让网关删掉这张卡片。"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import OutboxItem
from coreman.core.feishu_open import PATH, chat_biz, link
from tests.integration.worker_helpers import seed_bot


async def test_redirects_to_the_chat_applink(client: httpx.AsyncClient) -> None:
    response = await client.get(PATH, params={"chat": "oc_5d2f49e6"}, follow_redirects=False)
    assert response.status_code == 302
    assert (
        response.headers["location"]
        == "https://applink.feishu.cn/client/chat/open?openChatId=oc_5d2f49e6"
    )


async def test_redirects_to_the_bot_applink(client: httpx.AsyncClient) -> None:
    response = await client.get(PATH, params={"app": "cli_a1b2"}, follow_redirects=False)
    assert response.status_code == 302
    assert (
        response.headers["location"] == "https://applink.feishu.cn/client/bot/open?appId=cli_a1b2"
    )


async def test_rejects_anything_else(client: httpx.AsyncClient) -> None:
    for params in (
        {},
        {"chat": "https://example.com"},
        {"app": "x&y=1"},
        {"chat": "oc_a b"},
        {"chat": "oc_a\n"},
    ):
        response = await client.get(PATH, params=params, follow_redirects=False)
        assert response.status_code == 400, params


async def test_link_prefers_the_chat() -> None:
    base = "https://coreman.example.com/"
    assert link(base, chat_id="oc_1", app_id="cli_1") == (
        "https://coreman.example.com/api/feishu/open?chat=oc_1"
    )
    assert link(base, chat_id=None, app_id="cli_1") == (
        "https://coreman.example.com/api/feishu/open?app=cli_1"
    )


async def test_position_is_passed_through_when_it_is_a_number(client: httpx.AsyncClient) -> None:
    ok = await client.get(PATH, params={"chat": "oc_1", "pos": "272"}, follow_redirects=False)
    assert ok.headers["location"].endswith("openChatId=oc_1&position=272")
    odd = await client.get(PATH, params={"chat": "oc_1", "pos": "1&x=2"}, follow_redirects=False)
    assert odd.headers["location"].endswith("openChatId=oc_1")


async def test_signed_link_also_clears_the_card(
    client: httpx.AsyncClient, app: Any, db_session: AsyncSession
) -> None:
    bot, _, _ = await seed_bot(db_session)
    await db_session.commit()
    key = app.state.settings.master_key_bytes
    url = link("http://testserver", chat_id="oc_1", position="5", clear=(key, str(bot.id), 7, "u1"))
    params = dict(parse_qsl(urlsplit(url).query))
    forged = await client.get(PATH, params={**params, "u": "u2"}, follow_redirects=False)
    assert forged.status_code == 302
    assert (await db_session.execute(select(OutboxItem))).scalars().all() == []
    response = await client.get(PATH, params=params, follow_redirects=False)
    assert response.headers["location"].endswith("openChatId=oc_1&position=5")
    response = await client.get(PATH, params=params, follow_redirects=False)  # 点两次只删一次
    (item,) = (await db_session.execute(select(OutboxItem))).scalars().all()
    assert item.bot_id == bot.id and item.dedupe_key == "feed_click:7:u1"
    assert item.payload == {"_feed_delete": {"biz": chat_biz("oc_1"), "user": "u1"}}


async def test_malformed_clear_params_only_redirect(client: httpx.AsyncClient) -> None:
    base = {"chat": "oc_1", "b": "00000000-0000-0000-0000-000000000001", "u": "u1"}
    for extra in ({"d": "²", "s": "0" * 32}, {"d": "7", "s": "é"}, {"d": "7", "s": "x" * 32}):
        response = await client.get(PATH, params={**base, **extra}, follow_redirects=False)
        assert response.status_code == 302, extra

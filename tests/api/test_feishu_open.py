"""消息流卡片跳转：只 302 到飞书 AppLink 的打开会话 / 打开机器人，参数不合格式回 400。"""

from __future__ import annotations

import httpx

from coreman.core.feishu_open import PATH, link


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

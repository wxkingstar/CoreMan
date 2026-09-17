import uuid

import httpx

from coreman.core.bots.secrets import CREDENTIALS_AAD, encrypt_json
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot


async def test_group_names_only_resolve_known_ids_and_cache_safe_fallback(respx_mock):
    from coreman.core.cron.chats import known_chat_options

    cipher = Cipher(b"x" * 32)
    bot = Bot(
        id=uuid.uuid4(),
        platform="feishu",
        credentials_enc=encrypt_json(
            cipher, {"app_id": "task5", "app_secret": "synthetic"}, CREDENTIALS_AAD
        ),
    )
    respx_mock.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal").mock(
        return_value=httpx.Response(
            200, json={"code": 0, "tenant_access_token": "test", "expire": 3600}
        )
    )
    named = respx_mock.get("https://open.feishu.cn/open-apis/im/v1/chats/known").mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"name": "销售群"}})
    )
    denied = respx_mock.get("https://open.feishu.cn/open-apis/im/v1/chats/denied").mock(
        return_value=httpx.Response(403, json={"code": 99991672})
    )
    expected = [{"id": "known", "name": "销售群"}, {"id": "denied", "name": "denied"}]
    assert await known_chat_options(bot, ["known", "denied"], cipher) == expected
    assert await known_chat_options(bot, ["known", "denied"], cipher) == expected
    assert named.call_count == 1 and denied.call_count == 1

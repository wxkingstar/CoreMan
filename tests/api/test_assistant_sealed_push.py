"""推送接口的幂等重放：个人助理的出站载荷是密文，比对前要先还原。"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.crypto import Cipher
from tests.api.conftest import login_as
from tests.api.test_infra_actions import signed
from tests.integration.assistant_helpers import MASTER_KEY, SECRET, raw, seed_assistant

PATH = "/api/infra/push"


@pytest.fixture(autouse=True)
def _master() -> Iterator[None]:
    sealed.use_master(Cipher(MASTER_KEY))
    keys.forget()
    sealed.forget()
    yield
    sealed.use_master(None)
    keys.forget()
    sealed.forget()


async def test_replayed_push_to_an_assistant_compares_plaintext(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await login_as(client, db_session, role="platform_admin")
    credential = (
        await client.post(
            "/api/admin/api-clients",
            json={"app_key": "actions", "name": "Actions", "scopes": ["push"]},
        )
    ).json()["data"]
    bot, _ = await seed_assistant(db_session)
    body = {"bot_key": bot.bot_key, "chat_id": "oc_1", "content": SECRET, "request_id": "r1"}
    for _ in range(2):
        r = await client.post(PATH, json=body, headers=signed(PATH, body, credential["secret"]))
        assert r.status_code == 200, r.text
    stored = str(await raw(db_session, "SELECT string_agg(payload::text, '|') FROM outbox"))
    assert SECRET not in stored and stored.count("enc:v2:d1:") == 1
    changed = {**body, "content": f"{SECRET}-2"}
    r = await client.post(PATH, json=changed, headers=signed(PATH, changed, credential["secret"]))
    assert r.status_code == 409

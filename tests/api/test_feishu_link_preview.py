"""飞书链接预览回调：校验、管理台会话与任务链接按查看的人展开。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import ChatLog, User, UserIdentity, WorkTask
from coreman.core.feishu_link_preview import PATH
from tests.integration.worker_helpers import seed_bot

TOKEN = "verify-token"
APP = "cli_preview"


@pytest.fixture
def configured(app: Any) -> None:
    app.state.settings = app.state.settings.model_copy(
        update={
            "feishu_preview_app_id": APP,
            "feishu_preview_verification_token": SecretStr(TOKEN),
            "feishu_preview_encrypt_key": SecretStr("enc-key"),
        }
    )


def event(url: str, user_id: str = "fs_owner", token: str = TOKEN) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {"event_type": "url.preview.get", "token": token, "app_id": APP},
        "event": {
            "operator": {"user_id": user_id},
            "host": "im_message",
            "context": {"url": url, "preview_token": "p", "open_message_id": "om_1"},
        },
    }


def encrypt(body: dict[str, Any], key: str = "enc-key") -> dict[str, str]:
    iv = os.urandom(16)
    padder = padding.PKCS7(128).padder()
    data = padder.update(json.dumps(body).encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(hashlib.sha256(key.encode()).digest()), modes.CBC(iv)).encryptor()
    return {"encrypt": base64.b64encode(iv + enc.update(data) + enc.finalize()).decode()}


async def people(db_session: AsyncSession) -> tuple[User, User]:
    owner = User(login_name="owner1", display_name="甲")
    other = User(login_name="other1", display_name="乙")
    db_session.add_all([owner, other])
    await db_session.flush()
    db_session.add_all(
        [
            UserIdentity(user_id=owner.id, platform="feishu", platform_user_id="fs_owner"),
            UserIdentity(user_id=other.id, platform="feishu", platform_user_id="fs_other"),
        ]
    )
    await db_session.flush()
    return owner, other


async def test_unconfigured_returns_nothing(client: httpx.AsyncClient) -> None:
    response = await client.post(PATH, json=event("http://testserver/work-tasks/T-1"))
    assert response.status_code == 200 and response.json() == {}


async def test_url_verification_and_token_check(
    client: httpx.AsyncClient, configured: None
) -> None:
    ok = await client.post(
        PATH, json={"type": "url_verification", "token": TOKEN, "challenge": "c1"}
    )
    assert ok.json() == {"challenge": "c1"}
    bad = await client.post(PATH, json={"type": "url_verification", "token": "x", "challenge": "c"})
    assert bad.status_code == 403
    wrong = await client.post(PATH, json=event("http://testserver/work-tasks/T-1", token="x"))
    assert wrong.status_code == 403
    garbled = await client.post(PATH, json={"encrypt": "%%%"})
    assert garbled.status_code == 400


async def test_private_session_shows_only_to_its_owner(
    client: httpx.AsyncClient, configured: None, db_session: AsyncSession
) -> None:
    bot, _relay, _cipher = await seed_bot(db_session)
    owner, _other = await people(db_session)
    sid = uuid.uuid4()
    for _ in range(2):
        db_session.add(
            ChatLog(
                bot_id=bot.id,
                bot_key=bot.bot_key,
                platform="feishu",
                user_id=owner.id,
                chat_type="single",
                relay_session_id=sid,
                message_type="text",
                status="success",
                request_at=datetime.now(UTC),
            )
        )
    await db_session.commit()
    url = f"http://testserver/api/admin/runtime-nodes/{uuid.uuid4()}/claude/session/{sid}"
    mine = await client.post(PATH, json=encrypt(event(url)))
    assert mine.json() == {"inline": {"title": f"{bot.name} · 会话记录 · 2 轮"}}
    theirs = await client.post(PATH, json=event(url, user_id="fs_other"))
    assert theirs.json() == {"inline": {"title": "CoreMan 会话记录"}}
    elsewhere = await client.post(PATH, json=event(url.replace("testserver", "example.com")))
    assert elsewhere.json() == {}


async def test_task_link_shows_title_to_members(
    client: httpx.AsyncClient, configured: None, db_session: AsyncSession
) -> None:
    await people(db_session)
    task = WorkTask(title="整理上周退款", status="in_progress", platform="feishu", chat_id="oc1")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    url = f"http://testserver/work-tasks/{task.code}"
    member = await client.post(PATH, json=event(url))
    assert member.json() == {"inline": {"title": f"任务 {task.code} · 整理上周退款 · 进行中"}}
    stranger = await client.post(PATH, json=event(url, user_id="fs_nobody"))
    assert stranger.json() == {"inline": {"title": f"CoreMan 任务 {task.code}"}}

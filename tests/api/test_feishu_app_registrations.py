import base64
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json, encrypt_json
from coreman.core.crypto import Cipher
from coreman.core.db.models import AuditLog, Bot, FeishuAppRegistration, Team
from coreman.core.feishu_apps import management, registration
from coreman.core.platforms.feishu import FeishuClient
from tests.api.conftest import MASTER_KEY, login_as, login_existing

CIPHER = Cipher(base64.b64decode(MASTER_KEY))
URL = "https://open.feishu.cn/page/launcher?user_code=AB-CD&createOnly=true"


def _body(**over: object) -> dict[str, object]:
    body: dict[str, object] = {
        "bot_key": "feishu_sales",
        "platform": "feishu",
        "name": "销售助手",
        "description": "",
        "relay_server_id": None,
        "model": "claude-sonnet-5",
        "working_dir": "/data/skills/feishu_sales",
        "system_prompt": "",
        "verbosity_level": 1,
        "effort_level": None,
        "sse_timeout_seconds": 3600,
        "env_vars": {},
        "welcome_message": None,
        "enabled": True,
    }
    body.update(over)
    return body


class FakeFeishu:
    def __init__(self) -> None:
        self.begins: list[dict[str, object]] = []
        self.results: list[registration.Poll] = []
        self.default_commands: list[str] = []

    async def begin(self, **kwargs: object) -> registration.Begin:
        self.begins.append(kwargs)
        return registration.Begin(f"dc-{len(self.begins)}", URL, 1, 600)

    async def poll(self, device_code: str) -> registration.Poll:
        return self.results.pop(0) if self.results else registration.Poll("pending")

    async def ensure_default_commands(self, client: FeishuClient) -> list[str]:
        self.default_commands.append(client.app_id)
        return ["new", "stop", "sessions", "connect", "help"]


async def _member(client: httpx.AsyncClient, db_session: AsyncSession, name: str = "creator"):
    team = Team(slug=f"t{uuid.uuid4().hex[:6]}", name_zh="团队")
    db_session.add(team)
    await db_session.commit()
    return await login_as(client, db_session, role="member", team_id=team.id, login_name=name)


def _fake(monkeypatch) -> FakeFeishu:  # type: ignore[no-untyped-def]
    fake = FakeFeishu()
    monkeypatch.setattr(registration, "begin", fake.begin)
    monkeypatch.setattr(registration, "poll", fake.poll)
    monkeypatch.setattr(management, "ensure_default_commands", fake.ensure_default_commands)
    return fake


async def _due(db_session: AsyncSession, registration_id: str) -> None:
    row = await db_session.get(FeishuAppRegistration, uuid.UUID(registration_id))
    assert row is not None
    row.next_poll_at = None
    await db_session.commit()


async def test_scan_then_create_binds_credentials_without_exposing_secret(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    fake = _fake(monkeypatch)
    await _member(client, db_session)

    r = await client.post("/api/admin/bots", params={"dry_run": True}, json=_body())
    assert r.status_code == 200 and r.json()["data"] == {"valid": True}, r.text
    assert await db_session.scalar(select(func.count()).select_from(Bot)) == 0

    r = await client.post(
        "/api/admin/feishu-app-registrations",
        json={"purpose": "create", "name": "销售助手", "description": "卖货"},
    )
    assert r.status_code == 201, r.text
    started = r.json()["data"]
    assert started["status"] == "pending" and started["url"] == URL and not started["reused"]
    assert fake.begins[0]["name"] == "销售助手" and fake.begins[0]["app_id"] is None

    r = await client.get(f"/api/admin/feishu-app-registrations/{started['id']}")
    assert r.json()["data"]["status"] == "pending" and r.json()["data"]["retry_after"] == 1

    fake.results.append(registration.Poll("succeeded", "cli_new", "app-secret-value", "ou_owner"))
    await _due(db_session, started["id"])
    r = await client.get(f"/api/admin/feishu-app-registrations/{started['id']}")
    done = r.json()["data"]
    assert done["status"] == "succeeded" and done["app_id"] == "cli_new" and done["url"] is None
    assert "app-secret-value" not in r.text

    r = await client.post("/api/admin/bots", json=_body(feishu_registration_id=started["id"]))
    assert r.status_code == 201, r.text
    bot = (await db_session.execute(select(Bot))).scalar_one()
    assert decrypt_json(CIPHER, bot.credentials_enc, CREDENTIALS_AAD) == {
        "app_id": "cli_new",
        "app_secret": "app-secret-value",
    }
    bot_id = bot.id
    db_session.expire_all()
    row = await db_session.get(FeishuAppRegistration, uuid.UUID(started["id"]))
    assert row is not None
    assert (row.status, row.bot_id, row.secret_enc, row.owner_open_id) == (
        "consumed",
        bot_id,
        None,
        "ou_owner",
    )
    audit = await db_session.scalar(select(AuditLog).where(AuditLog.action == "bot.create"))
    assert audit is not None and "app-secret-value" not in str(audit.diff)
    # 扫码创建的智能体默认补齐 CoreMan 内置斜杠指令。
    assert fake.default_commands == ["cli_new"]

    # 已被消费的会话不能再建第二个员工。
    r = await client.post(
        "/api/admin/bots",
        json=_body(bot_key="other", working_dir="/data/o", feishu_registration_id=started["id"]),
    )
    assert r.status_code == 422


async def test_unused_app_is_reused_and_only_visible_to_its_initiator(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    fake = _fake(monkeypatch)
    owner = await _member(client, db_session, "owner")
    started = (
        await client.post("/api/admin/feishu-app-registrations", json={"purpose": "create"})
    ).json()["data"]
    fake.results.append(registration.Poll("succeeded", "cli_left", "s", ""))
    await _due(db_session, started["id"])
    await client.get(f"/api/admin/feishu-app-registrations/{started['id']}")

    again = (
        await client.post("/api/admin/feishu-app-registrations", json={"purpose": "create"})
    ).json()["data"]
    assert again["id"] == started["id"] and again["reused"] and again["app_id"] == "cli_left"
    assert len(fake.begins) == 1
    fresh = (
        await client.post(
            "/api/admin/feishu-app-registrations", json={"purpose": "create", "reuse": False}
        )
    ).json()["data"]
    assert fresh["id"] != started["id"] and fresh["status"] == "pending"

    await _member(client, db_session, "intruder")
    r = await client.get(f"/api/admin/feishu-app-registrations/{started['id']}")
    assert r.status_code == 404
    r = await client.post("/api/admin/bots", json=_body(feishu_registration_id=started["id"]))
    assert r.status_code == 404
    r = await client.delete(f"/api/admin/feishu-app-registrations/{started['id']}")
    assert r.status_code == 404

    await login_existing(client, db_session, owner)
    r = await client.delete(f"/api/admin/feishu-app-registrations/{started['id']}")
    assert r.json()["data"]["status"] == "cancelled"
    db_session.expire_all()
    row = await db_session.get(FeishuAppRegistration, uuid.UUID(started["id"]))
    assert row is not None and row.secret_enc is None


async def test_expired_denied_and_manual_credentials_are_rejected(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    fake = _fake(monkeypatch)
    await _member(client, db_session)
    started = (
        await client.post("/api/admin/feishu-app-registrations", json={"purpose": "create"})
    ).json()["data"]
    fake.results.append(registration.Poll("denied"))
    await _due(db_session, started["id"])
    r = await client.get(f"/api/admin/feishu-app-registrations/{started['id']}")
    assert r.json()["data"]["status"] == "denied"

    r = await client.post(
        "/api/admin/bots",
        json=_body(
            feishu_registration_id=started["id"],
            credentials={"app_id": "cli_x", "app_secret": "s"},
        ),
    )
    assert r.status_code == 422

    second = (
        await client.post("/api/admin/feishu-app-registrations", json={"purpose": "create"})
    ).json()["data"]
    row = await db_session.get(FeishuAppRegistration, uuid.UUID(second["id"]))
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    r = await client.get(f"/api/admin/feishu-app-registrations/{second['id']}")
    assert r.json()["data"]["status"] == "expired" and r.json()["data"]["url"] is None


async def test_dry_run_reports_conflicts_and_create_is_rate_limited(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    fake = _fake(monkeypatch)
    await _member(client, db_session)
    r = await client.post(
        "/api/admin/bots",
        json=_body(credentials={"app_id": "cli_manual", "app_secret": "manual-secret"}),
    )
    assert r.status_code == 201, r.text
    assert fake.default_commands == []  # 手动填写凭证的应用不自动改动其飞书配置
    r = await client.post("/api/admin/bots", params={"dry_run": True}, json=_body())
    assert r.status_code == 409
    for _ in range(10):
        r = await client.post(
            "/api/admin/feishu-app-registrations", json={"purpose": "create", "reuse": False}
        )
        assert r.status_code == 201
    r = await client.post(
        "/api/admin/feishu-app-registrations", json={"purpose": "create", "reuse": False}
    )
    assert r.status_code == 429
    rows = await db_session.scalars(select(FeishuAppRegistration.status))
    assert sorted(rows) == ["cancelled"] * 9 + ["pending"]


async def test_update_refreshes_rotated_secret_for_bot_admin_only(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    fake = _fake(monkeypatch)
    await _member(client, db_session)
    r = await client.post(
        "/api/admin/bots",
        json=_body(credentials={"app_id": "cli_bound", "app_secret": "old", "encrypt_key": "ek"}),
    )
    bot_id, created_version = r.json()["data"]["id"], r.json()["data"]["version"]
    r = await client.post(
        "/api/admin/feishu-app-registrations", json={"purpose": "update", "bot_id": bot_id}
    )
    assert r.status_code == 201, r.text
    started = r.json()["data"]
    assert fake.begins[-1]["app_id"] == "cli_bound"

    fake.results.append(registration.Poll("succeeded", "cli_bound", "rotated", "ou"))
    await _due(db_session, started["id"])
    r = await client.get(f"/api/admin/feishu-app-registrations/{started['id']}")
    assert r.json()["data"]["status"] == "consumed"
    db_session.expire_all()
    bot = await db_session.get(Bot, uuid.UUID(bot_id))
    assert bot is not None and bot.version == created_version + 1
    assert decrypt_json(CIPHER, bot.credentials_enc, CREDENTIALS_AAD) == {
        "app_id": "cli_bound",
        "app_secret": "rotated",
        "encrypt_key": "ek",
    }

    started = (
        await client.post(
            "/api/admin/feishu-app-registrations", json={"purpose": "update", "bot_id": bot_id}
        )
    ).json()["data"]
    fake.results.append(registration.Poll("succeeded", "cli_other", "x", "ou"))
    await _due(db_session, started["id"])
    r = await client.get(f"/api/admin/feishu-app-registrations/{started['id']}")
    assert r.json()["data"]["status"] == "failed" and r.json()["data"]["error"] == "app_mismatch"

    await _member(client, db_session, "stranger")
    r = await client.post(
        "/api/admin/feishu-app-registrations", json={"purpose": "update", "bot_id": bot_id}
    )
    assert r.status_code == 403


async def test_update_requires_feishu_bot_with_app(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    _fake(monkeypatch)
    user = await _member(client, db_session)
    bot = Bot(
        bot_key="wecom_bot",
        platform="wecom",
        name="企微",
        created_by=user.id,
        team_id=user.team_id,
        model="m",
        working_dir="/w",
        credentials_enc=encrypt_json(CIPHER, {"bot_id": "b", "secret": "s"}, CREDENTIALS_AAD),
    )
    db_session.add(bot)
    await db_session.commit()
    r = await client.post(
        "/api/admin/feishu-app-registrations", json={"purpose": "update", "bot_id": str(bot.id)}
    )
    assert r.status_code == 422


async def test_update_is_marked_failed_when_bot_switched_apps_during_scan(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    fake = _fake(monkeypatch)
    await _member(client, db_session)
    r = await client.post(
        "/api/admin/bots",
        json=_body(credentials={"app_id": "cli_first", "app_secret": "s1"}),
    )
    bot_id = r.json()["data"]["id"]
    started = (
        await client.post(
            "/api/admin/feishu-app-registrations", json={"purpose": "update", "bot_id": bot_id}
        )
    ).json()["data"]
    bot = await db_session.get(Bot, uuid.UUID(bot_id))
    assert bot is not None
    bot.credentials_enc = encrypt_json(
        CIPHER, {"app_id": "cli_second", "app_secret": "s2"}, CREDENTIALS_AAD
    )
    await db_session.commit()

    fake.results.append(registration.Poll("succeeded", "cli_first", "rotated", "ou"))
    await _due(db_session, started["id"])
    for _ in range(2):
        r = await client.get(f"/api/admin/feishu-app-registrations/{started['id']}")
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "failed"
        assert r.json()["data"]["error"] == "app_mismatch"
    db_session.expire_all()
    bot = await db_session.get(Bot, uuid.UUID(bot_id))
    assert bot is not None
    assert decrypt_json(CIPHER, bot.credentials_enc, CREDENTIALS_AAD)["app_id"] == "cli_second"

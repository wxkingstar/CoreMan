import base64
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.crypto import Cipher
from coreman.core.db.models import AlertState, AuditLog, Bot, Team, User, WecomBotProvision
from coreman.core.db.session import make_session_factory
from coreman.core.observability import alerts
from coreman.core.wecom_bots import scan, verify
from coreman.core.wecom_bots import service as provisions
from tests.api.conftest import MASTER_KEY, login_as, login_existing

CIPHER = Cipher(base64.b64decode(MASTER_KEY))
URL = "https://work.weixin.qq.com/ai/qc/c?s={}&hide_more_btn=true&for_native=true"
BASE = "/api/admin/wecom-bot-provisions"


def _body(**over: object) -> dict[str, object]:
    body: dict[str, object] = {
        "bot_key": "wecom_sales",
        "platform": "wecom",
        "name": "销售助手",
        "description": "",
        "relay_server_id": None,
        "model": "claude-sonnet-5",
        "working_dir": "/data/skills/wecom_sales",
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


class FakeScan:
    def __init__(self) -> None:
        self.generated: list[str] = []
        self.queried: list[str] = []
        self.results: list[scan.Query | scan.ScanError] = []
        self.generate_error: scan.ScanError | None = None

    async def generate(self) -> scan.Generated:
        if self.generate_error:
            raise self.generate_error
        scode = f"scode{len(self.generated):04d}xyz"
        self.generated.append(scode)
        return scan.Generated(scode, URL.format(scode))

    async def query(self, scode: str) -> scan.Query:
        self.queried.append(scode)
        result = self.results.pop(0) if self.results else scan.Query("waiting", "init")
        if isinstance(result, scan.ScanError):
            raise result
        return result


def _fake(monkeypatch: pytest.MonkeyPatch) -> FakeScan:
    fake = FakeScan()
    monkeypatch.setattr(scan, "generate", fake.generate)
    monkeypatch.setattr(scan, "query", fake.query)
    return fake


def _bound(bot_id: str = "aib-new", secret: str = "bot-secret-value") -> scan.Query:
    return scan.Query("succeeded", "success", bot_id, secret)


async def _member(client: httpx.AsyncClient, db_session: AsyncSession, name: str = "creator"):
    team = Team(slug=f"t{uuid.uuid4().hex[:6]}", name_zh="团队")
    db_session.add(team)
    await db_session.commit()
    return await login_as(client, db_session, role="member", team_id=team.id, login_name=name)


async def _row(db_session: AsyncSession, provision_id: str) -> WecomBotProvision:
    db_session.expire_all()
    row = await db_session.get(WecomBotProvision, uuid.UUID(provision_id))
    assert row is not None
    return row


async def _due(db_session: AsyncSession, provision_id: str) -> None:
    row = await _row(db_session, provision_id)
    row.next_poll_at = None
    await db_session.commit()


async def _start(client: httpx.AsyncClient, **body: object) -> dict[str, object]:
    r = await client.post(BASE, json=body)
    assert r.status_code == 201, r.text
    return r.json()["data"]  # type: ignore[no-any-return]


async def test_scan_then_create_binds_verified_credentials_without_exposing_secret(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    wecom_verified: list[tuple[str, str]],
) -> None:
    fake = _fake(monkeypatch)
    await _member(client, db_session)

    r = await client.post("/api/admin/bots", params={"dry_run": True}, json=_body())
    assert r.status_code == 200 and r.json()["data"] == {"valid": True}, r.text

    started = await _start(client)
    scode = fake.generated[0]
    assert started["status"] == "pending" and started["url"] == URL.format(scode)
    assert started["reused"] is False and "scode" not in started
    row = await _row(db_session, str(started["id"]))
    assert row.pending_enc and scode not in row.pending_enc
    assert row.expires_at - row.created_at <= timedelta(minutes=5, seconds=5)

    r = await client.get(f"{BASE}/{started['id']}")
    assert r.json()["data"]["status"] == "pending" and fake.queried == []

    await _due(db_session, str(started["id"]))
    r = await client.get(f"{BASE}/{started['id']}")
    assert r.json()["data"]["retry_after"] == provisions.POLL_SECONDS
    assert r.json()["data"]["upstream_status"] == "init"

    fake.results.append(_bound())
    await _due(db_session, str(started["id"]))
    r = await client.get(f"{BASE}/{started['id']}")
    done = r.json()["data"]
    assert done["status"] == "succeeded" and done["wecom_bot_id"] == "aib-new"
    assert done["verified"] is True and done["url"] is None
    assert "bot-secret-value" not in r.text and scode not in r.text
    assert wecom_verified == [("aib-new", "bot-secret-value")]
    row = await _row(db_session, str(started["id"]))
    # 拿到结果后删掉本地保存的 scode，只剩加密的 Secret 等待交付。
    assert row.pending_enc is None and row.secret_enc and row.verified_at

    r = await client.post("/api/admin/bots", json=_body(wecom_provision_id=started["id"]))
    assert r.status_code == 201, r.text
    assert "bot-secret-value" not in r.text
    bot = (await db_session.execute(select(Bot))).scalar_one()
    assert decrypt_json(CIPHER, bot.credentials_enc, CREDENTIALS_AAD) == {
        "bot_id": "aib-new",
        "secret": "bot-secret-value",
    }
    bot_id = bot.id
    row = await _row(db_session, str(started["id"]))
    assert row.status == "consumed" and row.bot_id == bot_id and row.secret_enc is None
    audit = await db_session.scalar(select(AuditLog).where(AuditLog.action == "bot.create"))
    assert audit is not None and audit.diff["wecom_provision_id"] == [None, started["id"]]
    # 刚校验过，不再重复占用长连接。
    assert len(wecom_verified) == 1

    # 同一个结果只能交付一次。
    r = await client.post(
        "/api/admin/bots",
        json=_body(
            bot_key="wecom_other", working_dir="/data/skills/o", wecom_provision_id=started["id"]
        ),
    )
    assert r.status_code == 422, r.text
    assert await db_session.scalar(select(func.count()).select_from(Bot)) == 1


async def test_provision_id_rules(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake(monkeypatch)
    await _member(client, db_session)
    started = await _start(client)
    r = await client.post(
        "/api/admin/bots",
        json=_body(wecom_provision_id=started["id"], credentials={"bot_id": "b", "secret": "s"}),
    )
    assert r.status_code == 422 and "不能再填写凭证" in r.json()["message"]
    r = await client.post(
        "/api/admin/bots", json=_body(platform="feishu", wecom_provision_id=started["id"])
    )
    assert r.status_code == 422
    # 还没扫码就建员工：没有可交付的凭证。
    r = await client.post("/api/admin/bots", json=_body(wecom_provision_id=started["id"]))
    assert r.status_code == 422 and "重新扫码" in r.json()["message"]


async def test_manual_credentials_go_through_the_same_verification(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    wecom_verified: list[tuple[str, str]],
) -> None:
    await _member(client, db_session)
    manual = _body(credentials={"bot_id": "aib-manual", "secret": "manual-secret"})

    async def rejected(bot_id: str, secret: str, **_: object) -> None:
        raise verify.VerifyError("rejected", 853000)

    monkeypatch.setattr(verify, "verify_credentials", rejected)
    r = await client.post("/api/admin/bots", json=manual)
    assert r.status_code == 422 and "853000" in r.json()["message"], r.text
    assert "manual-secret" not in r.text

    async def unavailable(bot_id: str, secret: str, **_: object) -> None:
        raise verify.VerifyError("unavailable")

    monkeypatch.setattr(verify, "verify_credentials", unavailable)
    r = await client.post("/api/admin/bots", json=manual)
    assert r.status_code == 502, r.text
    assert await db_session.scalar(select(func.count()).select_from(Bot)) == 0

    # dry_run 只校验员工配置，不连企业微信。
    r = await client.post("/api/admin/bots", params={"dry_run": True}, json=manual)
    assert r.status_code == 200, r.text

    async def ok(bot_id: str, secret: str, **_: object) -> None:
        wecom_verified.append((bot_id, secret))

    monkeypatch.setattr(verify, "verify_credentials", ok)
    r = await client.post("/api/admin/bots", json=manual)
    assert r.status_code == 201, r.text
    assert wecom_verified == [("aib-manual", "manual-secret")]


async def test_verification_failure_keeps_the_bot_for_a_retry(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake(monkeypatch)
    await _member(client, db_session)
    calls: list[str] = []
    outcome = {"code": "rejected"}

    async def flaky(bot_id: str, secret: str, **_: object) -> None:
        calls.append(bot_id)
        if outcome["code"]:
            raise verify.VerifyError(outcome["code"], 853000)

    monkeypatch.setattr(verify, "verify_credentials", flaky)
    started = await _start(client)
    fake.results.append(_bound())
    await _due(db_session, str(started["id"]))
    data = (await client.get(f"{BASE}/{started['id']}")).json()["data"]
    assert data["status"] == "succeeded" and data["verified"] is False
    assert data["error"] == "verify_rejected"

    r = await client.post("/api/admin/bots", json=_body(wecom_provision_id=started["id"]))
    assert r.status_code == 422 and "API 模式" in r.json()["message"]
    assert (await _row(db_session, str(started["id"]))).status == "succeeded"

    # 在企业微信后台开好长连接后重试即可，不用再扫码。
    outcome["code"] = ""
    r = await client.post("/api/admin/bots", json=_body(wecom_provision_id=started["id"]))
    assert r.status_code == 201, r.text
    assert calls == ["aib-new", "aib-new", "aib-new"]


async def test_only_the_initiator_sees_a_session_and_one_qr_per_person(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake(monkeypatch)
    await _member(client, db_session, "alice")
    first = await _start(client)
    second = await _start(client)
    old = await _row(db_session, str(first["id"]))
    assert old.status == "cancelled" and old.error == "superseded" and old.pending_enc is None

    await _member(client, db_session, "bob")
    assert (await client.get(f"{BASE}/{second['id']}")).status_code == 404
    assert (await client.delete(f"{BASE}/{second['id']}")).status_code == 404
    r = await client.post("/api/admin/bots", json=_body(wecom_provision_id=second["id"]))
    assert r.status_code == 404


async def test_starts_are_rate_limited_per_person(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake(monkeypatch)
    await _member(client, db_session)
    for _ in range(provisions.STARTS_PER_HOUR):
        await _start(client, reuse=False)
    r = await client.post(BASE, json={"reuse": False})
    assert r.status_code == 429


async def test_qr_expires_on_our_own_clock(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake(monkeypatch)
    await _member(client, db_session)
    started = await _start(client)
    row = await _row(db_session, str(started["id"]))
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    data = (await client.get(f"{BASE}/{started['id']}")).json()["data"]
    assert data["status"] == "expired" and data["error"] == "qr_expired" and data["url"] is None
    assert fake.queried == []
    assert (await _row(db_session, str(started["id"]))).pending_enc is None


async def test_network_errors_back_off_but_never_past_the_deadline(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake(monkeypatch)
    await _member(client, db_session)
    started = await _start(client)
    fake.results += [scan.ScanError("upstream_unavailable")] * 2
    await _due(db_session, str(started["id"]))
    data = (await client.get(f"{BASE}/{started['id']}")).json()["data"]
    assert data["status"] == "pending" and data["retry_after"] == 6

    row = await _row(db_session, str(started["id"]))
    row.next_poll_at, row.expires_at = None, datetime.now(UTC) + timedelta(seconds=4)
    await db_session.commit()
    await client.get(f"{BASE}/{started['id']}")
    row = await _row(db_session, str(started["id"]))
    assert row.poll_interval == 12 and row.next_poll_at == row.expires_at

    # 恢复后回到默认节奏。
    row.next_poll_at, row.expires_at = None, datetime.now(UTC) + timedelta(minutes=4)
    await db_session.commit()
    await client.get(f"{BASE}/{started['id']}")
    assert (await _row(db_session, str(started["id"]))).poll_interval == provisions.POLL_SECONDS


async def test_unexpected_responses_raise_an_alert(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake(monkeypatch)
    await _member(client, db_session)
    started = await _start(client)
    fake.results.append(scan.ScanError("unexpected_response"))
    await _due(db_session, str(started["id"]))
    data = (await client.get(f"{BASE}/{started['id']}")).json()["data"]
    assert data["status"] == "pending" and data["error"] == "unexpected_response"

    await alerts.tick(db_session, datetime.now(UTC))
    await db_session.commit()
    state = await db_session.get(AlertState, "wecom_provision")
    assert state is not None and state.firing and state.message_key == "alert_wecom_provision"

    # 生成二维码就失败：同样落一行 failed，计入限流与告警。
    fake.generate_error = scan.ScanError("unexpected_response")
    r = await client.post(BASE, json={"reuse": False})
    assert r.status_code == 502, r.text
    failed = await db_session.scalar(
        select(func.count())
        .select_from(WecomBotProvision)
        .where(
            WecomBotProvision.status == "failed", WecomBotProvision.error == "unexpected_response"
        )
    )
    assert failed == 1


async def test_switch_turns_off_new_qr_codes_but_keeps_finished_bots(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake(monkeypatch)
    creator_id = (await _member(client, db_session)).id
    started = await _start(client)
    fake.results.append(_bound())
    await _due(db_session, str(started["id"]))
    await client.get(f"{BASE}/{started['id']}")

    await login_as(client, db_session, role="platform_admin")
    r = await client.put("/api/admin/settings", json={"wecom_qr_provisioning_enabled": False})
    assert r.status_code == 200, r.text
    r = await client.get("/api/admin/settings/defaults")
    assert r.json()["data"]["wecom_qr_provisioning_enabled"] is False

    await _member(client, db_session, "other")
    r = await client.post(BASE, json={})
    assert r.status_code == 409 and "手动填写" in r.json()["message"]
    assert len(fake.generated) == 1

    # 已经扫码建好的机器人照常交付，不让它白白过期。
    creator = await db_session.get(User, creator_id)
    assert creator is not None
    await login_existing(client, db_session, creator)
    reused = await _start(client)
    assert reused["reused"] is True and reused["id"] == started["id"]


async def test_reuse_and_last_poll_on_close(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake(monkeypatch)
    await _member(client, db_session)
    started = await _start(client)
    # 刚扫完码就关窗口：关之前再查一次，建好的机器人留给下次。
    fake.results.append(_bound("aib-late"))
    r = await client.delete(f"{BASE}/{started['id']}")
    assert r.json()["data"]["status"] == "succeeded"

    again = await _start(client)
    assert again["reused"] is True and again["id"] == started["id"]
    assert again["wecom_bot_id"] == "aib-late" and len(fake.generated) == 1

    fresh = await _start(client, reuse=False)
    assert fresh["status"] == "pending" and len(fake.generated) == 2
    r = await client.delete(f"{BASE}/{fresh['id']}")
    assert r.json()["data"]["status"] == "cancelled"
    assert (await _row(db_session, str(fresh["id"]))).pending_enc is None


async def test_scheduler_sweep_collects_results_after_the_page_is_closed(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    wecom_verified: list[tuple[str, str]],
) -> None:
    fake = _fake(monkeypatch)
    await _member(client, db_session)
    waiting = await _start(client)
    await _member(client, db_session, "second")
    scanned = await _start(client)
    now = datetime.now(UTC)
    for provision_id, ago in ((waiting["id"], 10), (scanned["id"], 5)):
        row = await _row(db_session, str(provision_id))
        row.next_poll_at = now - timedelta(seconds=ago)
        await db_session.commit()
    fake.results += [scan.Query("waiting", "pending"), _bound("aib-swept")]

    factory = make_session_factory(db_engine)
    counts = await provisions.sweep(factory, CIPHER)
    assert counts["wecom_provisions_polled"] == 2
    swept = await _row(db_session, str(scanned["id"]))
    assert swept.status == "succeeded" and swept.verified_at and swept.pending_enc is None
    assert (await _row(db_session, str(waiting["id"]))).upstream_status == "pending"
    assert wecom_verified == [("aib-swept", "bot-secret-value")]

    # 暂存期一过，密钥不等人来访问就清掉。
    swept.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    counts = await provisions.sweep(factory, CIPHER)
    assert counts["wecom_secrets_expired"] == 1
    expired = await _row(db_session, str(scanned["id"]))
    assert expired.status == "expired" and expired.secret_enc is None

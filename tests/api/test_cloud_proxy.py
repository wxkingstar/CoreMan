"""阿里云签名代理端到端：能力令牌 → 判定 → Tower 核销 → 签名转发 → 审计。

阿里云用 respx 假冒（换掉代理的 transport）；Tower 是本机线程里的真 HTTP 服务，
这样 vendored tower_gate.redeem 的请求格式、X-API-Key 与错误码都走真实代码。
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import urllib.parse
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.cloud_proxy import aliyun, policy, service, tower_gate
from coreman.core.cloud_proxy.risk_policy import CloudProxyPolicy
from coreman.core.config import CLOUD_PROXY_PRODUCTS
from coreman.core.db.models import Bot, Task, User
from coreman.core.db.models.cloud_proxy import CloudCall, CloudPendingApproval
from tests.integration.worker_helpers import seed_bot

AK = "LTAI-test-proxy-ak"
SK = "test-proxy-secret"
SLB = "/api/runtime/cloud/aliyun/slb"
READ = "DescribeAccessControlListAttribute"
ACL = "acl-example-test"
POLICY = CloudProxyPolicy.model_validate(
    {
        "alidns": {"managed_zones": ["example.com"]},
        "mse": {"endpoint": "mse.cn-hangzhou.aliyuncs.com"},
        "slb": {"acls": {ACL: "test"}},
    }
)


def form(params: dict[str, str]) -> bytes:
    # 与 Skill 一致：按参数名字典序编码，批准后重发同一字节
    return urllib.parse.urlencode(sorted(params.items())).encode()


def slb_body(entry: str) -> bytes:
    entries = json.dumps([{"entry": entry, "comment": "tmp"}])
    return form({"Action": "AddAccessControlListEntry", "AclId": ACL, "AclEntrys": entries})


LOW = slb_body("8.8.8.8")
HIGH = slb_body("8.8.8.0/24")


class FakeTower:
    def __init__(self) -> None:
        self.requests: list[tuple[dict[str, Any], str]] = []
        self.reply: tuple[int, dict[str, Any]] = (200, {"approval_id": "apv-1"})


@pytest.fixture
def tower() -> Iterator[tuple[FakeTower, str]]:
    fake = FakeTower()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers["Content-Length"])
            payload = json.loads(self.rfile.read(n))
            fake.requests.append((payload, self.headers.get("X-API-Key", "")))
            status, body = fake.reply
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield fake, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def aliyun_mock(monkeypatch: pytest.MonkeyPatch) -> Iterator[respx.MockRouter]:
    with respx.MockRouter(assert_all_called=False) as router:
        monkeypatch.setattr(
            service, "transport_factory", lambda url: httpx.MockTransport(router.async_handler)
        )
        yield router


def configure(
    app: Any, tower_url: str = "http://127.0.0.1:9", risk_policy: CloudProxyPolicy | None = POLICY
) -> None:
    app.state.settings = app.state.settings.model_copy(
        update={
            "cloud_proxy_policy": risk_policy,
            "tower_url": tower_url,
            "tower_callback_secret": SecretStr("cb"),
            "tower_sender_key": SecretStr("sender"),
            "tower_target_keys": {p: SecretStr(f"key-{p}") for p in CLOUD_PROXY_PRODUCTS},
            "aliyun_proxy_access_key_id": SecretStr(AK),
            "aliyun_proxy_access_key_secret": SecretStr(SK),
        }
    )


async def turn(db_session: AsyncSession) -> tuple[Bot, User, Task]:
    bot, _, _ = await seed_bot(db_session)
    member = User(login_name="zhangsan", display_name="张三", email="zhangsan@example.test")
    db_session.add(member)
    await db_session.flush()
    task = Task(bot_id=bot.id, kind="chat", payload={}, status="running", session_key="sess-1")
    db_session.add(task)
    await db_session.commit()
    return bot, member, task


def bearer(app: Any, bot: Bot, member: User, task: Task) -> dict[str, str]:
    cap = policy.Capability(
        task_id=task.id,
        bot_id=bot.id,
        user_id=member.id,
        origin_kind="chat",
        session_key="sess-1",
        relay_session_id=None,
        event_id=42,
        cron_job_id=None,
    )
    token = policy.issue_capability(app.state.cipher, cap, ttl_seconds=1800)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def ctx(
    app: Any, db_session: AsyncSession, tower: tuple[FakeTower, str]
) -> tuple[Bot, User, Task, dict[str, str]]:
    configure(app, tower[1])
    bot, member, task = await turn(db_session)
    return bot, member, task, bearer(app, bot, member, task)


def ok_route(router: respx.MockRouter, host: str, body: dict[str, Any]) -> respx.Route:
    return router.post(f"https://{host}/").mock(return_value=httpx.Response(200, json=body))


def sent_form(route: respx.Route, i: int = -1) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(route.calls[i].request.content.decode()))


async def calls(db_session: AsyncSession) -> list[CloudCall]:
    db_session.expire_all()
    return list((await db_session.scalars(select(CloudCall).order_by(CloudCall.id))).all())


# 认证与入口 ------------------------------------------------------------------------------------


async def test_disabled_without_settings(client: httpx.AsyncClient) -> None:
    r = await client.post(SLB, content=LOW)
    assert r.status_code == 503 and r.json()["error"] == "cloud_proxy_disabled"


async def test_auth_and_origin(client: httpx.AsyncClient, app: Any, ctx: Any) -> None:
    *_, auth = ctx
    r = await client.post(SLB, content=LOW)
    assert r.status_code == 401
    r = await client.post(SLB, content=LOW, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    r = await client.post(SLB, content=LOW, headers={"Authorization": "Bearer " + "x" * 5000})
    assert r.status_code == 401
    r = await client.post(SLB, content=LOW, headers={**auth, "Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"] == "origin_not_allowed"


async def test_inactive_task_is_rejected(
    client: httpx.AsyncClient, db_session: AsyncSession, ctx: Any
) -> None:
    _, _, task, auth = ctx
    task.status = "succeeded"
    await db_session.commit()
    r = await client.post(SLB, content=LOW, headers=auth)
    assert r.status_code == 403 and r.json()["error"] == "task_not_active"


async def test_unknown_product_and_action(
    client: httpx.AsyncClient, db_session: AsyncSession, ctx: Any, aliyun_mock: respx.MockRouter
) -> None:
    *_, auth = ctx
    r = await client.post("/api/runtime/cloud/aliyun/ecs", content=LOW, headers=auth)
    assert r.status_code == 403 and r.json()["error"] == "product_not_allowed"
    r = await client.post(SLB, content=form({"Action": "DeleteLoadBalancer"}), headers=auth)
    assert r.status_code == 403 and r.json()["error"] == "action_not_allowed"
    assert not aliyun_mock.calls
    junk = "X" * 300 + "-1"
    r = await client.post(SLB, content=form({"Action": junk}), headers=auth)
    assert r.status_code == 403 and junk not in r.text
    r = await client.post(SLB, content=form({"Action": "Y" * 300}), headers=auth)
    assert r.status_code == 403 and "Y" * 300 not in r.text
    rows = await calls(db_session)
    assert [(c.action, c.risk, c.outcome, c.status_code) for c in rows] == [
        ("DeleteLoadBalancer", "write_high", "denied", 403),
        ("<invalid>", "write_high", "denied", 403),
        ("Y" * 64, "write_high", "denied", 403),
    ]


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (
            form({"Action": READ, "AccessKeyId": "x"}),
            "reserved_param",
        ),
        (
            form({"Action": READ, "signature": "x"}),
            "reserved_param",
        ),
        (form({"Action": READ, "Version": "1"}), "reserved_param"),
        (b"Action=DescribeAccessControlListAttribute&AclId=a&AclId=b", "duplicate_param"),
        (form({"AclId": "a"}), "missing_action"),
        (b"Action=DescribeAccessControlListAttribute&&AclId=a", "bad_body"),
        (b"Action=%FF", "bad_body"),
        # 读动作不跑参数白名单：小写或全大写的第二个 action 若被网关认了，读就变成写
        (
            b"Action=DescribeAccessControlListAttribute&action=AddAccessControlListEntry",
            "duplicate_param",
        ),
        (
            b"ACTION=AddAccessControlListEntry&Action=DescribeAccessControlListAttribute",
            "duplicate_param",
        ),
        (b"Action=DescribeAccessControlListAttribute&AclId=a&aclid=b", "duplicate_param"),
        # U+017F 的 casefold 是 s；空白、NUL 也不收
        ("Action=DescribeAccessControlListAttribute&ſignatureNonce=x".encode(), "bad_param_name"),
        (b"Action=DescribeAccessControlListAttribute&Version%20=2099-01-01", "bad_param_name"),
        (b"Action=DescribeAccessControlListAttribute&Version%00=2099-01-01", "bad_param_name"),
        (b"Action=DescribeAccessControlListAttribute&%EF%BB%BFVersion=1", "bad_param_name"),
    ],
)
async def test_bad_bodies(
    client: httpx.AsyncClient, ctx: Any, aliyun_mock: respx.MockRouter, body: bytes, code: str
) -> None:
    *_, auth = ctx
    r = await client.post(SLB, content=body, headers=auth)
    assert r.status_code == 400 and r.json()["error"] == code
    assert not aliyun_mock.calls


async def test_body_cap_and_query(client: httpx.AsyncClient, ctx: Any) -> None:
    *_, auth = ctx
    r = await client.post(SLB, content=b"A=" + b"x" * 262_144, headers=auth)
    assert r.status_code == 413
    r = await client.post(SLB + "?x=1", content=LOW, headers=auth)
    assert r.status_code == 400 and r.json()["error"] == "query_not_allowed"


# 读与低风险写 -----------------------------------------------------------------------------------


async def test_read_is_signed_and_forwarded(
    client: httpx.AsyncClient, db_session: AsyncSession, ctx: Any, aliyun_mock: respx.MockRouter
) -> None:
    *_, auth = ctx
    route = ok_route(aliyun_mock, "alidns.aliyuncs.com", {"Echo": AK, "TotalCount": 1})
    body = form({"Action": "DescribeDomainRecords", "DomainName": "example.com"})
    r = await client.post("/api/runtime/cloud/aliyun/alidns", content=body, headers=auth)
    assert r.status_code == 200, r.text
    # 回显的 AccessKeyId 不能回到 agent
    assert AK not in r.text and r.json() == {"Echo": service.REDACTED, "TotalCount": 1}
    sent = sent_form(route)
    assert sent["AccessKeyId"] == AK and sent["Version"] == "2015-01-09"
    assert sent["DomainName"] == "example.com" and sent["Action"] == "DescribeDomainRecords"
    unsigned = {k: v for k, v in sent.items() if k != "Signature"}
    expected = aliyun.sign_v1(
        {"Action": "DescribeDomainRecords", "DomainName": "example.com"},
        version="2015-01-09",
        access_key_id=AK,
        secret=SK,
        nonce=unsigned["SignatureNonce"],
        timestamp=unsigned["Timestamp"],
    )
    assert dict(urllib.parse.parse_qsl(expected))["Signature"] == sent["Signature"]
    [row] = await calls(db_session)
    assert (row.product, row.action, row.risk, row.outcome, row.status_code) == (
        "alidns",
        "DescribeDomainRecords",
        "read",
        "ok",
        200,
    )


async def test_low_risk_write_goes_straight_through(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    ctx: Any,
    aliyun_mock: respx.MockRouter,
    tower: tuple[FakeTower, str],
) -> None:
    *_, auth = ctx
    route = ok_route(aliyun_mock, "slb.aliyuncs.com", {"RequestId": "r1"})
    r = await client.post(SLB, content=LOW, headers=auth)
    assert r.status_code == 200, r.text
    assert sent_form(route)["AclEntrys"] == json.dumps([{"entry": "8.8.8.8", "comment": "tmp"}])
    assert not tower[0].requests
    [row] = await calls(db_session)
    assert (row.risk, row.outcome, row.request_hash) == ("write_low", "ok", None)


async def test_aliyun_5xx_is_502_without_retry(
    client: httpx.AsyncClient, db_session: AsyncSession, ctx: Any, aliyun_mock: respx.MockRouter
) -> None:
    *_, auth = ctx
    route = aliyun_mock.post("https://slb.aliyuncs.com/").mock(
        return_value=httpx.Response(503, json={"Code": "ServiceUnavailable"})
    )
    r = await client.post(SLB, content=LOW, headers=auth)
    assert r.status_code == 502 and r.json()["status"] == 503
    assert route.call_count == 1
    [row] = await calls(db_session)
    assert (row.outcome, row.status_code) == ("http_error", 503)


async def test_aliyun_unreachable(
    client: httpx.AsyncClient, db_session: AsyncSession, ctx: Any, aliyun_mock: respx.MockRouter
) -> None:
    *_, auth = ctx
    aliyun_mock.post("https://slb.aliyuncs.com/").mock(side_effect=httpx.ConnectError("down"))
    r = await client.post(SLB, content=LOW, headers=auth)
    assert r.status_code == 502 and r.json()["error"] == "aliyun_unreachable"
    [row] = await calls(db_session)
    assert row.outcome == "unreachable"


async def test_regional_product_without_endpoint_is_503(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    app: Any,
    ctx: Any,
    aliyun_mock: respx.MockRouter,
) -> None:
    *_, auth = ctx
    configure(app, risk_policy=None)
    body = form({"Action": "ListNacosConfigs", "NamespaceId": "ns"})
    r = await client.post("/api/runtime/cloud/aliyun/mse", content=body, headers=auth)
    assert r.status_code == 503 and r.json()["error"] == "product_not_configured"
    assert not aliyun_mock.calls
    assert await calls(db_session) == []


async def test_regional_product_uses_configured_endpoint(
    client: httpx.AsyncClient, ctx: Any, aliyun_mock: respx.MockRouter
) -> None:
    *_, auth = ctx
    route = ok_route(aliyun_mock, "mse.cn-hangzhou.aliyuncs.com", {"Configurations": []})
    body = form({"Action": "ListNacosConfigs", "NamespaceId": "ns"})
    r = await client.post("/api/runtime/cloud/aliyun/mse", content=body, headers=auth)
    assert r.status_code == 200, r.text
    assert sent_form(route)["Version"] == "2019-05-31"


# 高风险写 ---------------------------------------------------------------------------------------


async def test_write_without_policy_is_high_risk(
    client: httpx.AsyncClient, app: Any, ctx: Any, aliyun_mock: respx.MockRouter
) -> None:
    *_, auth = ctx
    configure(app, risk_policy=None)
    r = await client.post(SLB, content=LOW, headers=auth)
    assert r.status_code == 428, r.text
    assert "未配置 CLOUD_PROXY_POLICY.slb.acls，按高风险处理" in r.json()["risk_reasons"]
    assert not aliyun_mock.calls


async def test_high_risk_without_token_is_428_and_pending(
    client: httpx.AsyncClient, db_session: AsyncSession, ctx: Any, aliyun_mock: respx.MockRouter
) -> None:
    bot, member, task, auth = ctx
    r = await client.post(SLB, content=HIGH, headers=auth)
    assert r.status_code == 428, r.text
    data = r.json()
    assert set(data) == {
        "error",
        "target",
        "caller",
        "requester",
        "external_id",
        "url",
        "risk_reasons",
        "message",
    }
    assert data["error"] == "approval_required" and data["target"] == "aliyun-slb"
    assert data["caller"] == bot.bot_key and data["requester"] == "zhangsan"
    assert data["url"] == "http://testserver" + SLB
    assert (
        data["external_id"].startswith(f"cm-{task.id}-")
        and len(data["external_id"]) == len(f"cm-{task.id}-") + 16
    )
    assert any("/24" in reason for reason in data["risk_reasons"])
    assert not aliyun_mock.calls
    agent_hash = tower_gate.request_hash("POST", "aliyun-slb", data["url"], HIGH, bot.bot_key)
    ids = (task.id, bot.id, member.id)
    db_session.expire_all()
    pending = await db_session.get(CloudPendingApproval, data["external_id"])
    assert pending is not None
    assert (pending.task_id, pending.bot_id, pending.user_id) == ids
    assert (pending.session_key, pending.origin_event_id) == ("sess-1", 42)
    assert (pending.product, pending.action, pending.status) == (
        "slb",
        "AddAccessControlListEntry",
        "pending",
    )
    assert pending.request_hash == agent_hash
    [row] = await calls(db_session)
    assert (row.risk, row.outcome, row.status_code, row.request_hash) == (
        "write_high",
        "approval_required",
        428,
        agent_hash,
    )


async def test_write_without_login_name_is_rejected_before_risk_reads(
    client: httpx.AsyncClient, db_session: AsyncSession, ctx: Any, aliyun_mock: respx.MockRouter
) -> None:
    _, member, _, auth = ctx
    member.login_name = None
    await db_session.commit()
    r = await client.post(SLB, content=HIGH, headers=auth)
    assert r.status_code == 403, r.text
    assert r.json()["error"] == "no_login_name" and "login_name" in r.json()["message"]
    assert not aliyun_mock.calls
    db_session.expire_all()
    assert (await db_session.scalars(select(CloudPendingApproval))).first() is None
    [row] = await calls(db_session)
    assert (row.outcome, row.status_code) == ("denied", 403)


async def test_unreadable_state_is_high_risk(
    client: httpx.AsyncClient, ctx: Any, aliyun_mock: respx.MockRouter
) -> None:
    *_, auth = ctx
    # 判定要先读记录；读失败只能按高风险，且读也走同一个签名客户端
    route = aliyun_mock.post("https://alidns.aliyuncs.com/").mock(
        return_value=httpx.Response(500, json={})
    )
    body = form({"Action": "SetDomainRecordStatus", "RecordId": "1", "Status": "Disable"})
    r = await client.post("/api/runtime/cloud/aliyun/alidns", content=body, headers=auth)
    assert r.status_code == 428
    assert route.called
    assert all(
        sent_form(route, i)["Action"].startswith("Describe") for i in range(route.call_count)
    )


async def test_token_redeemed_then_forwarded(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    ctx: Any,
    aliyun_mock: respx.MockRouter,
    tower: tuple[FakeTower, str],
) -> None:
    bot, _, _, auth = ctx
    first = (await client.post(SLB, content=HIGH, headers=auth)).json()
    route = ok_route(aliyun_mock, "slb.aliyuncs.com", {"RequestId": "r1"})
    r = await client.post(SLB, content=HIGH, headers={**auth, "X-Tower-Approval": "twa_abc"})
    assert r.status_code == 200, r.text
    [(payload, api_key)] = tower[0].requests
    agent_hash = tower_gate.request_hash("POST", "aliyun-slb", first["url"], HIGH, bot.bot_key)
    assert payload == {"token": "twa_abc", "request_hash": agent_hash, "caller": bot.bot_key}
    assert api_key == "key-slb"
    assert route.call_count == 1
    db_session.expire_all()
    pending = await db_session.get(CloudPendingApproval, first["external_id"])
    assert pending is not None and (pending.status, pending.approval_id) == ("executed", "apv-1")
    rows = await calls(db_session)
    assert (rows[-1].outcome, rows[-1].approval_id, rows[-1].request_hash) == (
        "ok",
        "apv-1",
        agent_hash,
    )


async def test_redeem_rejection_passes_through(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    ctx: Any,
    aliyun_mock: respx.MockRouter,
    tower: tuple[FakeTower, str],
) -> None:
    *_, auth = ctx
    tower[0].reply = (403, {"error": "request_mismatch"})
    route = ok_route(aliyun_mock, "slb.aliyuncs.com", {})
    r = await client.post(SLB, content=HIGH, headers={**auth, "X-Tower-Approval": "twa_abc"})
    assert r.status_code == 403 and r.json() == {"error": "request_mismatch"}
    assert not route.called
    [row] = await calls(db_session)
    assert (row.outcome, row.status_code) == ("denied", 403)


async def test_tower_unreachable_is_503(
    client: httpx.AsyncClient,
    app: Any,
    db_session: AsyncSession,
    ctx: Any,
    aliyun_mock: respx.MockRouter,
) -> None:
    *_, auth = ctx
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    configure(app, f"http://127.0.0.1:{port}")
    route = ok_route(aliyun_mock, "slb.aliyuncs.com", {})
    r = await client.post(SLB, content=HIGH, headers={**auth, "X-Tower-Approval": "twa_abc"})
    assert r.status_code == 503 and r.json() == {"error": "tower_unavailable"}
    assert not route.called
    [row] = await calls(db_session)
    assert row.outcome == "unreachable"


async def test_repeated_428_reuses_pending_row(
    client: httpx.AsyncClient, db_session: AsyncSession, ctx: Any, aliyun_mock: respx.MockRouter
) -> None:
    *_, auth = ctx
    first = (await client.post(SLB, content=HIGH, headers=auth)).json()
    second = (await client.post(SLB, content=HIGH, headers=auth)).json()
    assert first["external_id"] == second["external_id"]
    db_session.expire_all()
    rows = (await db_session.scalars(select(CloudPendingApproval))).all()
    assert len(rows) == 1


async def test_redeemed_call_is_recorded_before_aliyun_returns(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    ctx: Any,
    aliyun_mock: respx.MockRouter,
) -> None:
    *_, auth = ctx
    first = (await client.post(SLB, content=HIGH, headers=auth)).json()
    # 出站中途出现任何意外（这里用异常模拟进程在出站时被打断）：令牌已消耗，记录必须已经提交
    aliyun_mock.post("https://slb.aliyuncs.com/").mock(side_effect=RuntimeError("killed"))
    with pytest.raises(RuntimeError):
        await client.post(SLB, content=HIGH, headers={**auth, "X-Tower-Approval": "twa_abc"})
    db_session.expire_all()
    pending = await db_session.get(CloudPendingApproval, first["external_id"])
    assert pending is not None and (pending.status, pending.approval_id) == ("executed", "apv-1")
    rows = await calls(db_session)
    assert (rows[-1].outcome, rows[-1].approval_id, rows[-1].risk) == (
        "unreachable",
        "apv-1",
        "write_high",
    )


async def test_redeemed_then_unreachable_updates_the_same_row(
    client: httpx.AsyncClient, db_session: AsyncSession, ctx: Any, aliyun_mock: respx.MockRouter
) -> None:
    *_, auth = ctx
    aliyun_mock.post("https://slb.aliyuncs.com/").mock(side_effect=httpx.ConnectError("down"))
    r = await client.post(SLB, content=HIGH, headers={**auth, "X-Tower-Approval": "twa_abc"})
    assert r.status_code == 502
    [row] = await calls(db_session)
    assert (row.outcome, row.status_code, row.approval_id) == ("unreachable", 502, "apv-1")


async def test_executed_marks_this_tasks_pending_row(
    client: httpx.AsyncClient,
    app: Any,
    db_session: AsyncSession,
    ctx: Any,
    aliyun_mock: respx.MockRouter,
) -> None:
    bot, member, _, auth = ctx
    other_task = Task(bot_id=bot.id, kind="chat", payload={}, status="running", session_key="s2")
    db_session.add(other_task)
    await db_session.commit()
    other_auth = bearer(app, bot, member, other_task)
    mine = (await client.post(SLB, content=HIGH, headers=auth)).json()
    theirs = (await client.post(SLB, content=HIGH, headers=other_auth)).json()
    assert mine["external_id"] != theirs["external_id"]
    ok_route(aliyun_mock, "slb.aliyuncs.com", {})
    r = await client.post(SLB, content=HIGH, headers={**auth, "X-Tower-Approval": "twa_abc"})
    assert r.status_code == 200
    db_session.expire_all()
    a = await db_session.get(CloudPendingApproval, mine["external_id"])
    b = await db_session.get(CloudPendingApproval, theirs["external_id"])
    assert a is not None and b is not None
    assert (a.status, b.status) == ("executed", "pending")


async def test_hash_path_follows_public_base_url_prefix(
    client: httpx.AsyncClient,
    app: Any,
    db_session: AsyncSession,
    ctx: Any,
    aliyun_mock: respx.MockRouter,
    tower: tuple[FakeTower, str],
) -> None:
    bot, _, _, auth = ctx
    # Caddy 剥掉 /coreman 前缀再转给 CoreMan；Agent 按 428 给的完整 url 提交审批
    app.state.settings = app.state.settings.model_copy(
        update={"public_base_url": "https://h.example/coreman/"}
    )
    first = (await client.post(SLB, content=HIGH, headers=auth)).json()
    assert first["url"] == "https://h.example/coreman" + SLB
    agent_hash = tower_gate.request_hash("POST", "aliyun-slb", first["url"], HIGH, bot.bot_key)
    db_session.expire_all()
    pending = await db_session.get(CloudPendingApproval, first["external_id"])
    assert pending is not None and pending.request_hash == agent_hash
    ok_route(aliyun_mock, "slb.aliyuncs.com", {})
    r = await client.post(SLB, content=HIGH, headers={**auth, "X-Tower-Approval": "twa_abc"})
    assert r.status_code == 200
    assert tower[0].requests[-1][0]["request_hash"] == agent_hash


async def test_slow_upstream_hits_total_timeout(
    client: httpx.AsyncClient,
    ctx: Any,
    aliyun_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    *_, auth = ctx
    monkeypatch.setattr(service, "TIMEOUT_SECONDS", 0.05)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, json={})

    aliyun_mock.post("https://slb.aliyuncs.com/").mock(side_effect=slow)
    r = await client.post(SLB, content=LOW, headers=auth)
    assert r.status_code == 502 and r.json()["error"] == "aliyun_unreachable"

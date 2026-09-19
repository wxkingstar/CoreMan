import contextvars
import io
import json
import logging

import httpx
import pytest
from uvicorn.logging import AccessFormatter

from coreman.core.logging import (
    configure_logging,
    get_logger,
    redact_url_credentials,
    scrub_secrets,
)


def test_json_line_has_required_fields(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(service="worker", instance="worker-a:host:1:1", level="INFO")
    get_logger("test").info("task_started", task_id=42)
    line = capsys.readouterr().out.strip().splitlines()[-1]
    rec = json.loads(line)
    assert rec["event"] == "task_started"
    assert rec["level"] == "info"
    assert rec["service"] == "worker"
    assert rec["instance"] == "worker-a:host:1:1"
    assert rec["task_id"] == 42
    assert "ts" in rec


def test_level_filter(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(service="api", instance="api-1", level="WARNING")
    get_logger("test").info("hidden")
    get_logger("test").warning("shown")
    out = capsys.readouterr().out
    assert "hidden" not in out
    assert "shown" in out


def test_scrub_secrets_processor() -> None:
    event = {
        "event": "x",
        "app_secret": "abc",
        "BOT_TOKEN_ERP": "jwt",
        "Authorization": "Bearer y",
        "password": "p",
        "bot_key": "keep-me",
    }
    out = scrub_secrets(None, "info", event)
    assert out["app_secret"] == "***"
    assert out["BOT_TOKEN_ERP"] == "***"
    assert out["Authorization"] == "***"
    assert out["password"] == "***"
    assert out["bot_key"] == "keep-me"


def test_unknown_level_falls_back_to_info(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(service="api", instance="api-1", level="BOGUS")
    get_logger("test").info("visible")
    get_logger("test").debug("hidden")
    out = capsys.readouterr().out
    assert "visible" in out
    assert "hidden" not in out


def test_fields_survive_fresh_context(capsys: pytest.CaptureFixture[str]) -> None:
    """uvicorn 的请求任务不继承 lifespan 的 contextvars；service/instance 必须由处理器
    静态注入，否则请求路径的日志会缺这两个字段。"""
    configure_logging(service="api", instance="api-1", level="INFO")
    contextvars.Context().run(lambda: get_logger("t").info("in_request"))
    rec = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rec["service"] == "api"
    assert rec["instance"] == "api-1"


def test_stream_parameter_redirects_output(capsys: pytest.CaptureFixture[str]) -> None:
    """stream 给出时日志一行都不能落到 stdout（CLI 的结果 JSON 独占 stdout）。"""
    buf = io.StringIO()
    configure_logging(service="cli", instance="contact-sync", level="INFO", stream=buf)
    get_logger("t").info("routed_elsewhere")
    assert capsys.readouterr().out == ""
    rec = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert rec["event"] == "routed_elsewhere" and rec["service"] == "cli"


def _mock_client(status: int = 200) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status)))


def test_httpx_request_line_is_never_emitted(capsys: pytest.CaptureFixture[str]) -> None:
    """httpx 的 INFO 请求行带完整 URL，查询串里的 access_token / code 会原样进日志；
    进程开 DEBUG 时也不能放出来。"""
    configure_logging(service="api", instance="api-1", level="DEBUG")
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
    assert not logging.getLogger("httpcore").isEnabledFor(logging.INFO)
    with _mock_client() as client:
        client.get(
            "https://api.example.com/cgi-bin/auth/getuserinfo",
            params={"code": "OAUTH-CODE", "access_token": "LIVE-TOKEN"},
        )
    out = capsys.readouterr().out
    assert "HTTP Request" not in out
    assert "LIVE-TOKEN" not in out and "OAUTH-CODE" not in out


def test_emitted_httpx_record_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(service="api", instance="api-1", level="INFO")
    url = httpx.URL("https://api.example.com/cgi-bin/gettoken?corpid=c1&corpsecret=CORP-SECRET")
    logging.getLogger("httpx").warning(
        'HTTP Request: %s %s "%s %d %s"', "GET", url, "HTTP/1.1", 200, "OK"
    )
    out = capsys.readouterr().out
    assert "CORP-SECRET" not in out
    assert "gettoken?corpid=c1&corpsecret=*** " in out


def test_status_error_url_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    """raise_for_status 的异常文本带请求 URL：error 字段与 log.exception 的 traceback 都要抹掉。"""
    configure_logging(service="worker", instance="worker-1", level="INFO")
    with _mock_client(404) as client:
        response = client.get(
            "https://api.example.com/cgi-bin/media/get?access_token=LIVE-TOKEN&media_id=m1"
        )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        get_logger("t").exception("download_failed", error=str(exc))
    out = capsys.readouterr().out
    # stdlib 在 JSON 行之后还会把 traceback 原样再印一遍，那一段也在 out 里一并检查。
    assert "LIVE-TOKEN" not in out
    assert "for url 'https://api.example.com/cgi-bin/media/get?access_token=***&media_id=m1'" in out
    rec = json.loads(next(line for line in out.splitlines() if line.startswith("{")))
    assert "media/get?access_token=***&media_id=m1" in rec["error"]
    assert "access_token=***&media_id=m1" in rec["exception"]


def test_preconfigured_handlers_are_redacted() -> None:
    """uvicorn 的访问日志自带处理器、不经根处理器：回调查询串里的 code 同样抹掉，访问格式不变。"""
    buf = io.StringIO()
    access = logging.getLogger("tests.uvicorn.access")
    handler = logging.StreamHandler(buf)
    handler.setFormatter(
        AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False)
    )
    access.addHandler(handler)
    access.propagate = False
    try:
        # 重复配置（每次 lifespan 都会调）不能把格式化器越包越多。
        configure_logging(service="api", instance="api-1", level="INFO")
        configure_logging(service="api", instance="api-1", level="INFO")
        access.info(
            '%s - "%s %s HTTP/%s" %d',
            "192.0.2.1:5000",
            "GET",
            "/api/auth/wecom/callback?code=OAUTH-CODE&state=s1",
            "1.1",
            302,
        )
    finally:
        access.removeHandler(handler)
    assert buf.getvalue().startswith(
        '192.0.2.1:5000 - "GET /api/auth/wecom/callback?code=***&state=s1 HTTP/1.1" 302'
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/cgi-bin/gettoken?corpid=c1&corpsecret=S", "/cgi-bin/gettoken?corpid=c1&corpsecret=***"),
        (
            "/cgi-bin/user/get?userid=u&access_token=T",
            "/cgi-bin/user/get?userid=u&access_token=***",
        ),
        ("/oauth/callback?code=C&state=s", "/oauth/callback?code=***&state=s"),
        ("/cgi-bin/webhook/send?key=K", "/cgi-bin/webhook/send?key=***"),
        ("/x?pre_auth_code=P&suite_ticket=Q#f", "/x?pre_auth_code=***&suite_ticket=***#f"),
        ("/x?APP_SECRET=S&Refresh_Token=R", "/x?APP_SECRET=***&Refresh_Token=***"),
        ("for url 'https://h/x?access_token=T'", "for url 'https://h/x?access_token=***'"),
        ('{"url": "https://h/x?access_token=T"}', '{"url": "https://h/x?access_token=***"}'),
        # 非凭证参数、不在查询串里的 key=value 原样保留。
        (
            "/x?errcode=0&qrcode=q&bot_key=b&media_id=m",
            "/x?errcode=0&qrcode=q&bot_key=b&media_id=m",
        ),
        ("code=C access_token=T", "code=C access_token=T"),
    ],
)
def test_redact_url_credentials(raw: str, expected: str) -> None:
    assert redact_url_credentials(raw) == expected

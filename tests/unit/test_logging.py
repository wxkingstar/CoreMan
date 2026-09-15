import contextvars
import io
import json

import pytest

from coreman.core.logging import configure_logging, get_logger, scrub_secrets


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

import httpx
import pytest

from coreman.core import notification_channels as channels
from coreman.core.cron.delivery import chunks
from coreman.core.crypto import Cipher
from coreman.core.db.models import OutboxItem, Setting


def test_chunks_preserve_unicode_and_limits() -> None:
    original = "你好🌟🇯🇵" * 5000
    result = chunks(original, 2048)
    assert "".join(result) == original
    assert all(len(part.encode()) <= 2048 for part in result)


async def test_webhook_endpoint_body_and_response(monkeypatch) -> None:
    cipher = Cipher(b"\x07" * 32)
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr(
        channels, "RegisteredTransport", lambda *a, **kw: httpx.MockTransport(handler)
    )
    item = OutboxItem(
        target={
            "url_enc": cipher.encrypt(
                "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test",
                "notifications.webhook_url",
            )
        },
        payload={"content": "hello"},
    )
    await channels.send_webhook(item, cipher)
    assert len(requests) == 1 and b'"msgtype":"markdown_v2"' in requests[0].content
    item.target = {
        "url_enc": cipher.encrypt(
            "https://unregistered.example.com/?key=test", "notifications.webhook_url"
        )
    }
    with pytest.raises(ValueError):
        await channels.send_webhook(item, cipher)
    assert len(requests) == 1


async def test_smtp_tls_html_escape_and_cancellation_cleanup(monkeypatch) -> None:
    cipher = Cipher(b"\x07" * 32)
    settings = Setting(
        key="notification_smtp",
        value={
            "enabled": True,
            "host": "smtp.example.com",
            "port": 465,
            "security": "tls",
            "username": "bot@example.com",
            "sender": "bot@example.com",
            "password_enc": cipher.encrypt("synthetic-password", "notifications.smtp_password"),
        },
    )

    class Session:
        async def get(self, *args):
            return settings

    class Socket:
        closed = False

        def close(self):
            self.closed = True

    sock = Socket()

    async def connect(host, port):
        assert host == "smtp.example.com" and port == 465
        return sock

    monkeypatch.setattr(channels, "pinned_smtp_socket", connect)
    sent = []

    class SMTP:
        def __init__(self, **kw):
            assert (
                kw["validate_certs"] is True
                and kw["use_tls"] is True
                and kw["hostname"] == "smtp.example.com"
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def login(self, user, password):
            assert password == "synthetic-password"

        async def send_message(self, message):
            sent.append(message)
            return {}, "ok"

        def close(self):
            pass

    monkeypatch.setattr(channels.aiosmtplib, "SMTP", SMTP)
    item = OutboxItem(
        id=1,
        target={"email": "recipient@example.com"},
        payload={"subject": "Result", "content": '<script>alert("test")</script>'},
    )
    await channels.send_email(Session(), item, cipher)
    assert sock.closed and len(sent) == 1
    assert "&lt;script&gt;" in sent[0].get_body(preferencelist=("html",)).get_content()
    assert sent[0]["Message-ID"] == "<coreman-outbox-1@example.com>"

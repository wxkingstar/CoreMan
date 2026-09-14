"""Webhook 与 SMTP 通知；传输凭证只在发送端解密。"""

from __future__ import annotations

import asyncio
import html
import socket
from email.message import EmailMessage
from urllib.parse import parse_qs, urlsplit

import aiosmtplib
import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.crypto import Cipher
from coreman.core.db.models import OutboxItem, Setting
from coreman.core.notifications import NotificationError
from coreman.core.relay.safe_transport import RegisteredTransport, allowed_address, validate_host


def validate_wecom_webhook(url: str) -> str:
    try:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname == "qyapi.weixin.qq.com"
            and parsed.path == "/cgi-bin/webhook/send"
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
            and parsed.port in (None, 443)
            and set(query) == {"key"}
            and len(query["key"]) == 1
            and bool(query["key"][0].strip())
            and not any(c.isspace() or ord(c) < 32 for c in url)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("请输入有效的企业微信群机器人 Webhook 地址（含 key）")
    return url


async def send_webhook(item: OutboxItem, cipher: Cipher) -> None:
    url = validate_wecom_webhook(
        cipher.decrypt(item.target["url_enc"], "notifications.webhook_url")
    )
    parsed = urlsplit(url)
    assert parsed.hostname is not None  # validate_wecom_webhook requires the host.
    async with httpx.AsyncClient(
        transport=RegisteredTransport(parsed.hostname, 443, scheme="https"),
        trust_env=False,
        follow_redirects=False,
        timeout=15,
    ) as client:
        async with client.stream(
            "POST",
            url,
            json={"msgtype": "markdown_v2", "markdown_v2": {"content": item.payload["content"]}},
        ) as response:
            response.raise_for_status()
            raw = b""
            async for part in response.aiter_bytes():
                raw += part
                if len(raw) > 65536:
                    raise ValueError("webhook response size limit")
            import json

            data = json.loads(raw)
            if data.get("errcode") != 0:
                # 不回传可能包含 webhook key 的平台错误正文。
                code = data.get("errcode")
                label = str(code) if isinstance(code, int) else "unknown"
                raise NotificationError(f"企微群通知被拒绝（错误码 {label}）")


async def send_email(session: AsyncSession, item: OutboxItem, cipher: Cipher) -> None:
    stored = await session.get(Setting, "notification_smtp")
    if stored is None or not stored.value.get("enabled"):
        raise ValueError("SMTP is not configured")
    cfg = stored.value
    hostname, port = cfg["host"], cfg["port"]
    sock = await pinned_smtp_socket(hostname, port)
    smtp = None
    try:
        # 已核验的 IP 建连接，不进行第二次 DNS；hostname 留给 TLS 校验与 SNI。
        smtp = aiosmtplib.SMTP(
            hostname=hostname,
            sock=sock,
            timeout=15,
            use_tls=cfg["security"] == "tls",
            start_tls=cfg["security"] == "starttls",
            validate_certs=True,
            local_hostname="coreman",
        )
        message = EmailMessage()
        message["From"] = cfg["sender"]
        message["To"] = item.target["email"]
        message["Subject"] = item.payload["subject"]
        # 重试沿用 Message-ID，邮件仍是至少一次语义，接收端不保证去重。
        message["Message-ID"] = f"<coreman-outbox-{item.id}@{cfg['sender'].split('@')[-1]}>"
        content = str(item.payload["content"])
        message.set_content(content)
        message.add_alternative(
            '<html><body><pre style="white-space:pre-wrap;font-family:sans-serif">'
            + html.escape(content)
            + "</pre></body></html>",
            subtype="html",
        )
        async with smtp:
            if cfg.get("username"):
                await smtp.login(
                    cfg["username"],
                    cipher.decrypt(cfg["password_enc"], "notifications.smtp_password"),
                )
            errors, _ = await smtp.send_message(message)
            if errors:
                raise ValueError("SMTP recipient rejected")
    finally:
        if smtp is not None:
            smtp.close()
        sock.close()


async def pinned_smtp_socket(hostname: str, port: int) -> socket.socket:
    validate_host(hostname)
    loop = asyncio.get_running_loop()
    addresses = await asyncio.wait_for(loop.getaddrinfo(hostname, port, type=socket.SOCK_STREAM), 5)
    if not addresses or any(not allowed_address(str(a[4][0])) for a in addresses):
        raise ValueError("SMTP destination not allowed")
    family, socktype, proto, _, address = addresses[0]
    sock = socket.socket(family, socktype, proto)
    sock.setblocking(False)
    try:
        await asyncio.wait_for(loop.sock_connect(sock, address), 5)
    except BaseException:
        sock.close()
        raise
    return sock

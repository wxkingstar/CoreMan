import base64
import re
import time
import uuid

from coreman.core import session_links
from coreman.core.crypto import Cipher

CIPHER = Cipher(b"\x07" * 32)


def _issue(**kw):
    values = {
        "session_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "node_id": uuid.uuid4(),
        "provider": "claude",
        "bot_id": uuid.uuid4(),
        **kw,
    }
    return values, session_links.issue(CIPHER, **values)


def test_round_trip_is_url_safe_and_opaque():
    values, token = _issue()
    assert re.fullmatch(r"[A-Za-z0-9_-]+", token)
    assert str(values["user_id"]) not in token
    claims = session_links.read(CIPHER, token)
    assert claims is not None
    assert claims.session_id == values["session_id"]
    assert claims.user_id == values["user_id"]
    assert claims.node_id == values["node_id"]
    assert claims.bot_id == values["bot_id"]
    assert claims.provider == "claude"


def test_expires_after_a_day():
    now = time.time()
    _, token = _issue(now=now)
    assert session_links.read(CIPHER, token, now=now + session_links.TTL_SECONDS - 1)
    assert session_links.read(CIPHER, token, now=now + session_links.TTL_SECONDS) is None


def test_rejects_tampering_other_keys_and_other_purposes():
    _, token = _issue()
    raw = bytearray(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    raw[-1] ^= 1
    tampered = base64.urlsafe_b64encode(bytes(raw)).decode().rstrip("=")
    other_key = Cipher(b"\x08" * 32)
    # 同一把主密钥加密的其它凭据（AAD 不同）也不能冒充查看链接。
    foreign = CIPHER.encrypt('{"s":"x"}', "feishu_personal.task_capability.v1")
    foreign_token = base64.urlsafe_b64encode(
        base64.b64decode(foreign.removeprefix("enc:v1:"))
    ).decode()
    for bad in (tampered, "", "x" * 2000, "!!!", foreign_token):
        assert session_links.read(CIPHER, bad) is None
    assert session_links.read(other_key, token) is None

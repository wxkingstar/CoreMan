import uuid

import pytest

from coreman.core.cloud_proxy import policy
from coreman.core.crypto import Cipher
from coreman.core.systems_catalog import policy as systems_policy

CIPHER = Cipher(b"\x07" * 32)


def _cap() -> policy.Capability:
    return policy.Capability(
        task_id=42,
        bot_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        origin_kind="chat",
        session_key="sk",
        relay_session_id=uuid.uuid4(),
        event_id=7,
        cron_job_id=None,
    )


def test_roundtrip() -> None:
    cap = _cap()
    assert (
        policy.read_capability(CIPHER, policy.issue_capability(CIPHER, cap, ttl_seconds=60)) == cap
    )


def test_cron_roundtrip_with_empty_optionals() -> None:
    cap = policy.Capability(1, uuid.uuid4(), uuid.uuid4(), "cron", None, None, None, uuid.uuid4())
    assert (
        policy.read_capability(CIPHER, policy.issue_capability(CIPHER, cap, ttl_seconds=60)) == cap
    )


def test_expired() -> None:
    token = policy.issue_capability(CIPHER, _cap(), ttl_seconds=-1)
    with pytest.raises(ValueError, match="expired_capability"):
        policy.read_capability(CIPHER, token)


def test_aad_isolation() -> None:
    systems = systems_policy.issue_capability(
        CIPHER, task_id=1, user_id=uuid.uuid4(), ttl_seconds=60
    )
    with pytest.raises(Exception):  # noqa: B017 解密失败的具体异常类型不是契约
        policy.read_capability(CIPHER, systems)
    with pytest.raises(Exception):  # noqa: B017
        systems_policy.read_capability(
            CIPHER, policy.issue_capability(CIPHER, _cap(), ttl_seconds=60)
        )


def test_apply_env_replaces_cloud_keys_and_builds_url() -> None:
    cap = _cap()
    env = policy.apply_env(
        {"COREMAN_CLOUD_OTHER": "old", "A": "1"},
        CIPHER,
        cap,
        public_base_url="https://cm.example.com/",
        sse_timeout_seconds=100,
    )
    assert env["A"] == "1" and "COREMAN_CLOUD_OTHER" not in env
    assert env[policy.URL_ENV] == "https://cm.example.com/api/runtime/cloud"
    assert policy.read_capability(CIPHER, env[policy.TOKEN_ENV]) == cap

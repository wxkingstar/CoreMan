"""个人凭证的校验规则：键名、请求体、提交值、卡片 ID 与本轮令牌。"""

import uuid

import pytest

from coreman.core.crypto import Cipher
from coreman.core.personal_credentials import policy
from coreman.core.prompting.env_vars import is_reserved_key

CIPHER = Cipher(b"\x07" * 32)


@pytest.mark.parametrize(
    "key,ok",
    [
        ("DEMO_API_KEY", True),
        ("demo_key", False),
        ("1KEY", False),
        ("COREMAN_ANYTHING", False),
        ("BOT_TOKEN_DEMO", False),
        ("PATH", False),
        ("ANTHROPIC_BASE_URL", False),
        ("A" * 65, False),
    ],
)
def test_key_rules(key, ok):
    assert (policy.key_problem(key) is None) is ok


def test_credential_prefix_is_reserved():
    assert is_reserved_key("COREMAN_CREDENTIAL_TOKEN")


def test_parse_request_accepts_agent_choice_and_rejects_bad_shapes():
    body = policy.parse_request(
        {
            "fields": [
                {"key": "DEMO_USERNAME", "label": "账号", "secret": False},
                {"key": "DEMO_PASSWORD", "label": "密码"},
            ],
            "purpose": "登录 Demo 系统",
        }
    )
    assert [f.secret for f in body.fields] == [False, True]
    with pytest.raises(policy.CredentialError) as dup:
        policy.parse_request({"fields": [{"key": "A_B", "label": "x"}] * 2, "purpose": "p"})
    assert dup.value.code == "invalid_fields"
    with pytest.raises(policy.CredentialError):
        policy.parse_request({"fields": [{"key": "PATH", "label": "x"}], "purpose": "p"})
    with pytest.raises(policy.CredentialError):
        policy.parse_request({"fields": [], "purpose": "p"})
    with pytest.raises(policy.CredentialError):
        policy.parse_request(
            {"fields": [{"key": f"K_{i}", "label": "x"} for i in range(21)], "purpose": "p"}
        )


def test_clean_values_requires_every_field_single_line_within_limit():
    fields = [{"key": "DEMO_PIN", "label": "PIN"}]
    assert policy.clean_values(fields, {"DEMO_PIN": "  abc123 ", "EXTRA": "x"}) == {
        "DEMO_PIN": "abc123"
    }
    for bad in ({}, {"DEMO_PIN": "  "}, {"DEMO_PIN": "a\nb"}, {"DEMO_PIN": "x" * 1001}):
        with pytest.raises(policy.CredentialError) as exc:
            policy.clean_values(fields, bad, limit=1000)
        assert exc.value.code == "invalid_values"


def test_card_task_id_round_trip():
    rid = uuid.uuid4()
    assert policy.parse_card_task_id(policy.card_task_id(rid)) == rid
    assert policy.parse_card_task_id("credential@not-a-uuid") is None
    assert policy.parse_card_task_id("personal:1") is None


def test_capability_round_trip_and_expiry():
    cap = policy.Capability(
        task_id=7,
        bot_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        origin_kind="chat",
        chat_id="oc_private",
        chat_type="single",
        session_key="oc_private",
        event_id=11,
        cron_job_id=None,
    )
    token = policy.issue_capability(CIPHER, cap, ttl_seconds=60)
    assert policy.read_capability(CIPHER, token) == cap
    expired = policy.issue_capability(CIPHER, cap, ttl_seconds=-1)
    with pytest.raises(ValueError):
        policy.read_capability(CIPHER, expired)


def test_value_aad_binds_row_identity():
    bot, user = uuid.uuid4(), uuid.uuid4()
    token = CIPHER.encrypt("secret-value", policy.value_aad(bot, user, "DEMO_PIN"))
    assert CIPHER.decrypt(token, policy.value_aad(bot, user, "DEMO_PIN")) == "secret-value"
    with pytest.raises(ValueError):
        CIPHER.decrypt(token, policy.value_aad(bot, uuid.uuid4(), "DEMO_PIN"))
    with pytest.raises(ValueError):
        CIPHER.decrypt(token, policy.value_aad(bot, user, "OTHER_KEY"))

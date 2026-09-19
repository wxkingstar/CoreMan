"""出站密钥闸门。"""

from coreman.core.chat.redaction import (
    PLACEHOLDER,
    collect_secrets,
    is_secret_key,
    redact,
    redact_text,
)

JWT = "eyJhbGciOiJFUzI1NiIsImtpZCI6ImsxIn0.eyJzdWIiOiJhbGljZSJ9.c2lnbmF0dXJlLWhlcmU"


def test_identity_keys_are_never_treated_as_secrets() -> None:
    """名字里带 KEY 的身份标识不能被当成密钥，否则正文里的人名和会话号会被打成方块。"""
    for key in ("COREMAN_BOT_KEY", "BOT_KEY", "COREMAN_USER_LOGIN", "COREMAN_SESSION_ID"):
        assert not is_secret_key(key)
    for key in (
        "BOT_TOKEN_ERP",
        "COREMAN_COLLABORATION_TOKEN",
        "COREMAN_FEISHU_PERSONAL_TOKEN",
        "MY_DB_PASSWORD",
        "SOME_API_KEY",
    ):
        assert is_secret_key(key)


def test_collect_skips_short_values() -> None:
    env = {"BOT_TOKEN_ERP": JWT, "X_TOKEN": "short", "COREMAN_BOT_KEY": "sales-bot-key"}
    assert collect_secrets(env) == frozenset({JWT})


def test_redacts_this_turn_credentials_and_jwt_shapes() -> None:
    secrets = frozenset({"super-secret-db-password"})
    text = "连接串用的是 super-secret-db-password，令牌是 " + JWT
    out = redact(text, secrets)
    assert "super-secret-db-password" not in out
    assert JWT not in out
    assert out.count(PLACEHOLDER) == 2


def test_longer_secret_wins_so_no_half_secret_survives() -> None:
    secrets = frozenset({"abcdefgh", "abcdefgh-ijklmnop"})
    assert redact("值 abcdefgh-ijklmnop 结束", secrets) == f"值 {PLACEHOLDER} 结束"


def test_leaves_ordinary_text_alone() -> None:
    text = "今天的销售额是 12345，负责人是张三（login=zhangsan）。"
    assert redact(text, frozenset({"unused-secret-value"})) == text
    assert redact("", frozenset({"x" * 20})) == ""
    assert redact(None, frozenset({"x" * 20})) is None


def test_boundaries_move_with_the_replacement() -> None:
    """切分点是正文上的下标：替换改变串长，不跟着挪就会整体偏移。"""
    secret = "super-secret-token"
    text = f"前言{secret}结尾"
    r = redact_text(text, frozenset({secret}))
    assert r.text == f"前言{PLACEHOLDER}结尾"
    # 密钥之前的下标不动；之后的下标按占位符与密钥的长度差平移。
    assert r.shift(0) == 0
    assert r.shift(2) == 2
    assert r.shift(len(text)) == len(r.text)
    assert r.text[r.shift(len(text) - 2) :] == "结尾"
    # 没有命中时下标原样返回。
    plain = redact_text("普通文本", frozenset({secret}))
    assert plain.text == "普通文本" and plain.shift(3) == 3


def test_overlapping_hits_collapse_into_one_placeholder() -> None:
    """一个密钥是另一个的子串、或字面量与 JWT 规则命中同一段时，只留一个占位符。"""
    out = redact(JWT, frozenset({JWT, JWT[:30]}))
    assert out == PLACEHOLDER

"""进程配置：只读 .env / 环境变量中的基础设施项。业务配置在 settings 表。"""

from __future__ import annotations

import base64
from functools import lru_cache
from typing import Literal

from pydantic import Field, PrivateAttr, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from coreman.core.auth.external_key import ExternalKey, load_external_key
from coreman.core.crypto import DEFAULT_KEY_ID, KID_RE, Cipher

# 引导管理员直接拿到 platform_admin。.env.example 的占位值只由 `deploy/coreman up` 替换，
# 直接 `docker compose up` 或自建编排时仍可能原样生效，这里在进程启动时拒绝。
BOOTSTRAP_PASSWORD_MIN_LENGTH = 12
TEMPLATE_PASSWORDS = frozenset({"change-me", "changeme"})


def _parse_previous_keys(raw: str, *, current: str) -> dict[str, bytes]:
    """`kid:base64,kid:base64` → `{kid: key}`；报错信息不带密钥内容。"""
    keys: dict[str, bytes] = {}
    for entry in (part.strip() for part in raw.split(",")):
        if not entry:
            continue
        kid, sep, value = entry.partition(":")
        if not sep or not KID_RE.match(kid):
            raise ValueError("MASTER_KEYS_PREVIOUS 的每一项都必须是 kid:base64 密钥")
        if kid == current:
            raise ValueError(f"MASTER_KEYS_PREVIOUS 中的 {kid} 与 MASTER_KEY_ID 重复")
        if kid in keys:
            raise ValueError(f"MASTER_KEYS_PREVIOUS 中的 {kid} 重复")
        try:
            decoded = base64.b64decode(value, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"MASTER_KEYS_PREVIOUS 中 {kid} 的密钥不是 base64") from exc
        if len(decoded) != 32:
            raise ValueError(f"MASTER_KEYS_PREVIOUS 中 {kid} 的密钥解码后必须是 32 字节")
        keys[kid] = decoded
    return keys


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    database_url: str = Field(alias="DATABASE_URL")
    public_base_url: str = Field(alias="PUBLIC_BASE_URL")
    master_key: str = Field(alias="MASTER_KEY")
    # 主密钥的标识，写进新密文（enc:v2:<kid>:...）。所有进程必须配同一个值。
    master_key_id: str = Field(default=DEFAULT_KEY_ID, alias="MASTER_KEY_ID")
    # 轮换期间保留的历史密钥，`kid:base64` 逗号分隔，只用于解密旧行。
    master_keys_previous: str = Field(default="", alias="MASTER_KEYS_PREVIOUS")
    session_secret: str = Field(alias="SESSION_SECRET", min_length=16)
    bootstrap_admin_username: str | None = Field(default=None, alias="BOOTSTRAP_ADMIN_USERNAME")
    bootstrap_admin_password: str | None = Field(default=None, alias="BOOTSTRAP_ADMIN_PASSWORD")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    object_storage: Literal["local", "s3"] = Field(default="local", alias="OBJECT_STORAGE")
    object_storage_root: str = Field(default="/data/storage", alias="OBJECT_STORAGE_ROOT")
    s3_endpoint: str | None = Field(default=None, alias="S3_ENDPOINT")
    s3_region: str = Field(default="us-east-1", alias="S3_REGION")
    s3_bucket: str | None = Field(default=None, alias="S3_BUCKET")
    s3_prefix: str = Field(default="coreman-objects", alias="S3_PREFIX")
    s3_access_key: SecretStr | None = Field(default=None, alias="S3_ACCESS_KEY")
    s3_secret_key: SecretStr | None = Field(default=None, alias="S3_SECRET_KEY")
    s3_session_token: SecretStr | None = Field(default=None, alias="S3_SESSION_TOKEN")
    runtime_bundle_dir: str = Field(default="runtime_daemon/dist", alias="RUNTIME_BUNDLE_DIR")
    web_dist_dir: str = Field(default="/app/web/dist", alias="WEB_DIST_DIR")
    coreman_env: Literal["dev", "prod"] = Field(default="prod", alias="COREMAN_ENV")
    service_name: str = Field(default="api", alias="COREMAN_SERVICE")
    instance_name: str = Field(default="", alias="COREMAN_INSTANCE_NAME")
    image_tag: str = Field(default="dev", alias="COREMAN_IMAGE_TAG")
    # 可选：沿用已有签发方的密钥签业务系统令牌，让已信任该签发方的系统不改动即可验签。
    bot_jwt_private_key: SecretStr | None = Field(default=None, alias="BOT_JWT_PRIVATE_KEY")
    bot_jwt_public_key: str | None = Field(default=None, alias="BOT_JWT_PUBLIC_KEY")
    bot_jwt_kid: str | None = Field(default=None, alias="BOT_JWT_KID")
    bot_jwt_issuer: str | None = Field(default=None, alias="BOT_JWT_ISSUER")
    _external_jwt_key: ExternalKey | None = PrivateAttr(default=None)
    _previous_master_keys: dict[str, bytes] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _load_external_jwt_key(self) -> Settings:
        # 进程启动时就解析，配错（缺 kid、公私钥不配对、非 P-256）直接起不来，而不是到签发时才失败。
        if self.bot_jwt_private_key is None or not self.bot_jwt_private_key.get_secret_value():
            if self.bot_jwt_public_key or self.bot_jwt_kid or self.bot_jwt_issuer:
                raise ValueError("BOT_JWT_* 已配置但缺少 BOT_JWT_PRIVATE_KEY")
            return self
        self._external_jwt_key = load_external_key(
            self.bot_jwt_private_key.get_secret_value(),
            public_pem=self.bot_jwt_public_key,
            kid=self.bot_jwt_kid,
            issuer=self.bot_jwt_issuer,
        )
        return self

    @property
    def external_jwt_key(self) -> ExternalKey | None:
        return self._external_jwt_key

    @model_validator(mode="after")
    def _check_storage_credentials(self) -> Settings:
        # pydantic 在每次加载配置时自动调用（代码里没有显式调用方，但不是死代码）。
        # S3ObjectStore 只在首次读写附件时才构造，这里让缺凭据的 S3 配置在进程启动时就失败。
        if self.object_storage == "s3" and not (
            self.s3_bucket and self.s3_access_key and self.s3_secret_key
        ):
            raise ValueError("S3 storage requires S3_BUCKET, S3_ACCESS_KEY and S3_SECRET_KEY")
        return self

    @model_validator(mode="after")
    def _check_bootstrap_password(self) -> Settings:
        # 口令留空等同关闭引导登录（auth 路由要求用户名与口令都非空），不拦。
        password = self.bootstrap_admin_password
        if not self.bootstrap_admin_username or not password:
            return self
        if password.strip().lower() in TEMPLATE_PASSWORDS:
            raise ValueError(
                "BOOTSTRAP_ADMIN_PASSWORD 仍是示例占位值，请改为独立强口令"
                "（deploy/coreman up 会自动生成）"
            )
        if len(password) < BOOTSTRAP_PASSWORD_MIN_LENGTH:
            raise ValueError(
                f"BOOTSTRAP_ADMIN_PASSWORD 至少需要 {BOOTSTRAP_PASSWORD_MIN_LENGTH} 个字符"
            )
        return self

    @field_validator("public_base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("master_key")
    @classmethod
    def _check_master_key(cls, v: str) -> str:
        try:
            raw = base64.b64decode(v, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("MASTER_KEY 必须是 base64") from exc
        if len(raw) != 32:
            raise ValueError("MASTER_KEY 解码后必须是 32 字节")
        return v

    @field_validator("master_key_id")
    @classmethod
    def _check_master_key_id(cls, v: str) -> str:
        if not KID_RE.match(v):
            raise ValueError("MASTER_KEY_ID 只能是 1-64 位字母、数字或 ._-")
        return v

    @model_validator(mode="after")
    def _check_previous_master_keys(self) -> Settings:
        # 进程启动时就解析：格式写错要立刻起不来，而不是等到读某一行旧密文时才失败。
        self._previous_master_keys = _parse_previous_keys(
            self.master_keys_previous, current=self.master_key_id
        )
        return self

    @property
    def master_key_bytes(self) -> bytes:
        return base64.b64decode(self.master_key)

    @property
    def previous_master_keys(self) -> dict[str, bytes]:
        return dict(self._previous_master_keys)

    def build_cipher(self) -> Cipher:
        """所有进程都从这里造 Cipher，主密钥、标识与历史密钥不会各配各的。"""
        return Cipher(
            self.master_key_bytes,
            key_id=self.master_key_id,
            previous=self.previous_master_keys,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # 字段由环境变量提供


def reset_settings_cache() -> None:
    get_settings.cache_clear()

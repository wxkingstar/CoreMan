"""进程配置：只读 .env / 环境变量中的基础设施项。业务配置在 settings 表。"""

from __future__ import annotations

import base64
from functools import lru_cache
from typing import Literal

from pydantic import Field, PrivateAttr, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from coreman.core.auth.external_key import ExternalKey, load_external_key

# 引导管理员直接拿到 platform_admin。.env.example 的占位值只由 `deploy/coreman up` 替换，
# 直接 `docker compose up` 或自建编排时仍可能原样生效，这里在进程启动时拒绝。
BOOTSTRAP_PASSWORD_MIN_LENGTH = 12
TEMPLATE_PASSWORDS = frozenset({"change-me", "changeme"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    database_url: str = Field(alias="DATABASE_URL")
    public_base_url: str = Field(alias="PUBLIC_BASE_URL")
    master_key: str = Field(alias="MASTER_KEY")
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

    @property
    def master_key_bytes(self) -> bytes:
        return base64.b64decode(self.master_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # 字段由环境变量提供


def reset_settings_cache() -> None:
    get_settings.cache_clear()

"""进程配置：只读 .env / 环境变量中的基础设施项（spec §4.2）。业务配置在 settings 表。"""

from __future__ import annotations

import base64
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    timezone: str = Field(default="Asia/Shanghai", alias="TIMEZONE")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    relay_network_mode: Literal["bridge", "host"] = Field(
        default="bridge", alias="RELAY_NETWORK_MODE"
    )
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

    @model_validator(mode="after")
    def storage_credentials(self) -> Settings:
        if self.object_storage == "s3" and not (
            self.s3_bucket and self.s3_access_key and self.s3_secret_key
        ):
            raise ValueError("S3 storage requires S3_BUCKET, S3_ACCESS_KEY and S3_SECRET_KEY")
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

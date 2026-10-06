"""Deployment-only delegated-token endpoints. Never expose these credentials to agents."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class HTTPTokenProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    token_url: str = Field(max_length=2048)
    client_id: str = Field(min_length=1, max_length=256)
    client_secret: SecretStr = Field(min_length=1)
    max_token_ttl_seconds: int | None = Field(default=None, gt=0, strict=True)
    subject_field: str = Field(default="username", pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")
    audience_field: str = Field(default="audience", pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")
    ttl_field: str = Field(default="expires_in", pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")

    @field_validator("token_url")
    @classmethod
    def secure_endpoint(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or any(c.isspace() for c in value)
        ):
            raise ValueError("token_url must be HTTPS without credentials, query or fragment")
        _ = url.port
        return value

    @model_validator(mode="after")
    def distinct_fields(self) -> HTTPTokenProviderConfig:
        names = {self.subject_field, self.audience_field, self.ttl_field}
        if len(names) != 3 or names & {"client_id", "client_secret", "grant_type"}:
            raise ValueError(
                "request fields must be distinct and cannot override client credentials"
            )
        return self

"""云签名代理的部署参数（设置项 CLOUD_PROXY_POLICY）：风险判定要用的域名、地址、实例与环境。

这些值因部署而异，不写进代码。缺哪一项，依赖它的写操作就按高风险处理（fail closed）。
"""

from __future__ import annotations

import ipaddress
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_HOST_LABELS = re.compile(
    r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+"
)
# 签名后的请求会发往这个主机，只认阿里云 MSE 的公网地域 endpoint
_MSE_ENDPOINT = re.compile(r"mse\.[a-z0-9-]+\.aliyuncs\.com")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _nonempty(values: tuple[str, ...] | dict[str, str], what: str) -> None:
    keys = values if isinstance(values, tuple) else tuple(values) + tuple(values.values())
    if any(not v or v != v.strip() for v in keys):
        raise ValueError(f"{what} 不能有空值或首尾空白")


class AlidnsPolicy(_Strict):
    # 允许修改的权威域名（小写、不带结尾的点）
    managed_zones: tuple[str, ...] = ()
    # CNAME 指向这些后缀（以 . 开头）或允许修改的域名本身时算可信目标
    trusted_cname_suffixes: tuple[str, ...] = ()
    # A / AAAA 指向这些公网地址时算可信目标；私网地址本来就算可信
    trusted_ips: tuple[str, ...] = ()

    @field_validator("managed_zones")
    @classmethod
    def _zones(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for zone in v:
            if not _HOST_LABELS.fullmatch(zone):
                raise ValueError(f"managed_zones 里的 {zone!r} 不是小写域名")
        return v

    @field_validator("trusted_cname_suffixes")
    @classmethod
    def _suffixes(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        # 不以 . 开头的后缀会让 evilexample.com 命中 example.com
        for suffix in v:
            if not suffix.startswith(".") or not _HOST_LABELS.fullmatch(suffix[1:]):
                raise ValueError(
                    f"trusted_cname_suffixes 里的 {suffix!r} 必须是以 . 开头的小写域名"
                )
        return v

    @field_validator("trusted_ips")
    @classmethod
    def _ips(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for ip in v:
            # 规则拿记录值的原文比对，只收规范写法，免得 IPv6 的不同写法对不上
            if str(ipaddress.ip_address(ip)) != ip:
                raise ValueError(f"trusted_ips 里的 {ip!r} 不是规范写法的 IP")
        return v


class MsePolicy(_Strict):
    # MSE 是地域服务，没有全局 endpoint；不配时 mse 的读写都不可用
    endpoint: str | None = None
    # 唯一允许写的 Nacos 实例
    instance_id: str | None = None
    # 命名空间 ID → 环境名
    namespaces: dict[str, str] = Field(default_factory=dict)
    # 更新这些环境的配置时只做内容检查（不删键、能解析），通过即低风险；其余登记环境一律审批
    low_risk_envs: tuple[str, ...] = ()

    @field_validator("endpoint")
    @classmethod
    def _endpoint(cls, v: str | None) -> str | None:
        if v is not None and not _MSE_ENDPOINT.fullmatch(v):
            raise ValueError("mse.endpoint 必须形如 mse.<地域>.aliyuncs.com")
        return v

    @model_validator(mode="after")
    def _envs(self) -> MsePolicy:
        if self.instance_id is not None:
            _nonempty((self.instance_id,), "mse.instance_id")
        _nonempty(self.namespaces, "mse.namespaces")
        unknown = set(self.low_risk_envs) - set(self.namespaces.values())
        if unknown:
            raise ValueError(f"mse.low_risk_envs 里有未登记的环境：{', '.join(sorted(unknown))}")
        return self


class SlbPolicy(_Strict):
    # 允许加白的访问控制列表 ID → 环境名（只用于说明）
    acls: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _acls(self) -> SlbPolicy:
        _nonempty(self.acls, "slb.acls")
        return self


class CloudProxyPolicy(_Strict):
    alidns: AlidnsPolicy = AlidnsPolicy()
    mse: MsePolicy = MsePolicy()
    slb: SlbPolicy = SlbPolicy()

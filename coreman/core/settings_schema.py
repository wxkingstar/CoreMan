"""settings 表中管理台可改的键与校验。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from coreman.core.db.models import ModelCatalog
from coreman.core.prompting.defaults import PROMPT_DEFAULTS_BY_KEY
from coreman.core.relay.models import default_model

# 提示词段落的键，顺序即管理台设置页的展示顺序，也是 worker 读取的顺序。
PROMPT_SETTING_KEYS: tuple[str, ...] = tuple(PROMPT_DEFAULTS_BY_KEY)

SETTING_DEFAULTS: dict[str, Any] = {
    "bootstrap_admin_enabled": True,
    "default_verbosity_level": 1,
    "default_effort_level": None,
    "session_ttl_hours": 72,
    "jwt_issuer": "coreman",
    # 全局并发闸门：worker 同时在跑的任务上限，其中 fast 车道独占的名额。
    "max_concurrent_tasks": 30,
    "fast_lane_slots": 2,
    "card_icon_url": "",
    # 提示词段落：默认值由 CoreMan 提供，管理台可逐段覆盖。
    **PROMPT_DEFAULTS_BY_KEY,
}
# 派生键：读取时由其它表算出，不落 settings 表、不可写入。库里遗留的同名行一律忽略。
DERIVED_SETTING_KEYS = ("default_model",)
# 平台默认模型按这个 provider 顺序取第一个有默认值的。
DEFAULT_MODEL_PROVIDERS = ("claude", "codex")
# 任何登录用户都能读的三个键：新建机器人表单要用它们做默认值（default_model 为派生键）。
PUBLIC_DEFAULT_KEYS = ("default_model", "default_verbosity_level", "default_effort_level")


def platform_default_model(catalog: Sequence[ModelCatalog]) -> str | None:
    """平台默认模型：按 claude → codex 取该 provider 的默认（未退役），目录为空时为 None。

    默认值只在模型目录维护一处（is_default / retired），这里不再有部署相关的硬编码模型名。
    """
    for provider in DEFAULT_MODEL_PROVIDERS:
        model = default_model(provider, catalog)
        if model is not None:
            return model
    return None


class SettingsPatch(BaseModel):
    bootstrap_admin_enabled: bool | None = None
    default_verbosity_level: int | None = Field(default=None, ge=1, le=4)
    default_effort_level: Literal["low", "medium", "high", "xhigh"] | None = None
    session_ttl_hours: int | None = Field(default=None, ge=1, le=720)
    jwt_issuer: str | None = Field(default=None, pattern=r"^[a-z0-9._-]{1,50}$")
    max_concurrent_tasks: int | None = Field(default=None, ge=1, le=500)
    fast_lane_slots: int | None = Field(default=None, ge=0, le=50)
    # 卡片来源图标：空串 = 不放图标（企微 source.icon_url 省略）；只认 http(s) 绝对地址。
    card_icon_url: str | None = Field(default=None, max_length=512, pattern=r"^(https?://\S+)?$")
    # 提示词段落：min_length=1 挡住「存成空串」——空串不会被 _check_not_null 当成置空，
    # 却会让安全策略/运行模式整段消失。20000 字符够放最长的 codex 输出契约还有余量。
    prompt_security_policy: str | None = Field(default=None, min_length=1, max_length=20000)
    prompt_codex_contract: str | None = Field(default=None, min_length=1, max_length=20000)
    prompt_runtime_mode: str | None = Field(default=None, min_length=1, max_length=20000)
    prompt_cron_mode: str | None = Field(default=None, min_length=1, max_length=20000)
    prompt_runtime_tail: str | None = Field(default=None, min_length=1, max_length=20000)
    prompt_verbosity_2: str | None = Field(default=None, min_length=1, max_length=20000)
    prompt_verbosity_3: str | None = Field(default=None, min_length=1, max_length=20000)
    prompt_verbosity_4: str | None = Field(default=None, min_length=1, max_length=20000)

    @model_validator(mode="before")
    @classmethod
    def _reject_derived(cls, data: Any) -> Any:
        """未知键照旧忽略（兼容旧前端多带的字段），但派生键必须明确拒绝：
        静默吞掉会让管理员以为默认模型改成功了。"""
        if isinstance(data, dict):
            derived = [k for k in DERIVED_SETTING_KEYS if k in data]
            if derived:
                raise ValueError(
                    "、".join(derived)
                    + " 由模型目录派生，不能在设置中修改，请到模型目录调整默认模型"
                )
        return data

    def changes(self) -> dict[str, Any]:
        """只取请求体里真正出现过的字段：None 是合法取值（default_effort_level 可以清空），
        不能用「值为 None」来判断「没传」。"""
        return self.model_dump(include=self.model_fields_set)

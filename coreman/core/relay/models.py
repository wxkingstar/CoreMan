"""模型目录的读取、有效模型集与默认模型。

`load_catalog` 原先住在 `coreman/api/routers/model_catalog.py`，限流自动切换
把它拉了进来：worker 不该为了读一张目录表去 import FastAPI 路由模块。原位置再导出。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import EFFORT_LEVELS, ModelCatalog, RelayServer

# 官方文档里各模型在 high 之上还支持哪些思考档位（2026-09-19 核对）。
# Claude：platform.claude.com/docs/en/build-with-claude/effort。Claude Code 的 --effort 用同一套
#   档位，模型不支持时降到不超过它的最高档（Opus 4.6 的 xhigh 按 high 跑）；Opus 4.5 只有
#   low/medium/high，Haiku 4.5、Sonnet 4.5 不支持 effort 参数。
# Codex：developers.openai.com/api/docs/models/<模型名>，Codex CLI 0.154 起认 max。
# 目录新增模型时据此预填 supports_xhigh / supports_max；没收录的模型默认都不支持，由管理员在
# 目录里勾选。迁移 0043 按这张表修正了已有目录行，这里改动不会回写已有行。
_XHIGH_MAX = frozenset({"xhigh", "max"})
_XHIGH = frozenset({"xhigh"})
_MAX = frozenset({"max"})
_NONE: frozenset[str] = frozenset()
EXTRA_EFFORTS: dict[str, frozenset[str]] = {
    "claude-fable-5-1": _XHIGH_MAX,
    "claude-fable-5": _XHIGH_MAX,
    "claude-mythos-5-1": _XHIGH_MAX,
    "claude-mythos-5": _XHIGH_MAX,
    "claude-mythos-preview": _MAX,
    "claude-opus-5": _XHIGH_MAX,
    "claude-opus-4-8": _XHIGH_MAX,
    "claude-opus-4-7": _XHIGH_MAX,
    "claude-opus-4-6": _MAX,
    "claude-opus-4-5": _NONE,
    "claude-sonnet-5": _XHIGH_MAX,
    "claude-sonnet-4-6": _MAX,
    "claude-sonnet-4-5": _NONE,
    "claude-haiku-4-5": _NONE,
    "gpt-6-astra": _XHIGH_MAX,
    "gpt-5.6-sol": _XHIGH_MAX,
    "gpt-5.6-terra": _XHIGH_MAX,
    "gpt-5.6-luna": _XHIGH_MAX,
    "gpt-5.5": _XHIGH,
    "gpt-5.4": _XHIGH,
    "gpt-5.4-mini": _XHIGH,
    "gpt-5.3-codex": _XHIGH,
    "gpt-5.2": _XHIGH,
}
# 快照日期后缀：claude-haiku-4-5-20251001、gpt-5.2-2025-12-11。
_SNAPSHOT_SUFFIX = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")


def backend_of(model: str, provider: str | None = None) -> Literal["claude", "codex"]:
    """按模型名前缀判断后端：codex/ 开头走 codex，其余走 claude。"""
    if provider in {"claude", "codex"}:
        return "codex" if provider == "codex" else "claude"
    return "codex" if model.startswith("codex/") else "claude"


def catalog_sorted(rows: Sequence[ModelCatalog]) -> list[ModelCatalog]:
    """目录展示顺序：sort_order 降序、model 升序。"""
    return sorted(rows, key=lambda r: (-r.sort_order, r.model))


def effective_models(relay: RelayServer, catalog: Sequence[ModelCatalog]) -> list[str]:
    """relay 的有效模型集：inherit 取 provider 下未退役的全部，restricted 取白名单与目录的交集。"""
    mine = [r for r in catalog_sorted(catalog) if r.provider == relay.model_provider]
    if relay.supported_models_mode == "restricted":
        known = {r.model for r in mine}  # 含已退役
        return [m for m in (relay.supported_models or []) if m in known]
    return [r.model for r in mine if not r.retired]


def default_model(provider: str, catalog: Sequence[ModelCatalog]) -> str | None:
    """provider 的默认模型：优先未退役且 is_default，否则排序后的第一个。"""
    mine = [r for r in catalog_sorted(catalog) if r.provider == provider and not r.retired]
    for r in mine:
        if r.is_default:
            return r.model
    return mine[0].model if mine else None


def supports_effort(model: str, effort: str, catalog: Sequence[ModelCatalog]) -> bool:
    """该模型是否支持这个思考档位：xhigh / max 看目录标记，其余档位都支持。

    同一 model 可挂在多个 provider 下，任一行支持即可。
    """
    if effort == "xhigh":
        return any(r.model == model and r.supports_xhigh for r in catalog)
    if effort == "max":
        return any(r.model == model and r.supports_max for r in catalog)
    return True


def fit_effort(model: str, effort: str, catalog: Sequence[ModelCatalog]) -> str:
    """模型支持的、不超过 effort 的最高档位（与 Claude Code 对不支持档位的处理一致）。"""
    for level in reversed(EFFORT_LEVELS[: EFFORT_LEVELS.index(effort) + 1]):
        if supports_effort(model, level, catalog):
            return level
    return effort


def known_extra_efforts(model: str) -> frozenset[str]:
    """按官方文档，模型在 high 之上还支持的档位；目录新增行时预填 supports_xhigh / supports_max。

    驱动调 CLI 前会去掉最后一个 `/` 之前的部分（`codex/`、`vllm/`），快照日期后缀也一并去掉。
    """
    name = _SNAPSHOT_SUFFIX.sub("", model.rsplit("/", 1)[-1])
    return EXTRA_EFFORTS.get(name, _NONE)


def known_effort_flags(model: str) -> dict[str, bool]:
    """目录行的 supports_xhigh / supports_max 预填值。"""
    extra = known_extra_efforts(model)
    return {"supports_xhigh": "xhigh" in extra, "supports_max": "max" in extra}


async def load_catalog(session: AsyncSession, provider: str | None = None) -> list[ModelCatalog]:
    """目录行（可按 provider 过滤），按 sort_order 降序、model 升序。relay/bots 路由复用。"""
    stmt = select(ModelCatalog)
    if provider:
        stmt = stmt.where(ModelCatalog.provider == provider)
    return catalog_sorted((await session.execute(stmt)).scalars().all())

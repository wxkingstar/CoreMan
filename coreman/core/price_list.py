"""内置的官方模型价格表，单位 USD / 百万 token。

成本估算先查这张表，再叠加管理员在「使用统计 → 模型价格」里录入的价格（同一模型同一生效日
以管理员录入的为准）。管理员录入适合临时修正或协议价；官方调价请改这里，随发布生效，API 启动后
会把还没有成本的历史记录按新价格补算。

维护规则：
- 官方调价时**追加**一行新的生效日，不要改旧行：旧价格还要用来估算调价前的记录。
- 改动后更新 VERIFIED_ON，并保留来源链接，方便下一次核对。
- 模型名写成目录里的样子（Codex 带 `codex/` 前缀）；匹配时忽略 `vllm/` 这类前缀、快照日期后缀
  和 `[1m]` 之类的标记，见 `canonical_model`。
- 缓存写入取 5 分钟 TTL 的价格；OpenAI 不对缓存写入单独计费，记 0。
- 分档计价的模型（Haiku 5.5 超 10 万 token 的提示、GPT 超 27.2 万 token 的上下文）取基础档：
  一轮对话的用量是多次请求的合计，看不出单次请求落在哪一档，长上下文会被低估。

来源：
- Claude: https://platform.claude.com/docs/en/about-claude/pricing
- OpenAI: https://developers.openai.com/api/docs/pricing
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from coreman.core.relay.models import backend_of

VERIFIED_ON = date(2026, 11, 17)
# 初始价格的生效日取一个早于平台上线的日期：核对时的官方价格回溯适用于全部历史记录。
_SINCE = date(2025, 1, 1)


@dataclass(frozen=True)
class BuiltinPrice:
    model: str
    effective_from: date
    input_usd: Decimal
    output_usd: Decimal
    cache_read_usd: Decimal
    cache_write_usd: Decimal

    @property
    def provider(self) -> str:
        return backend_of(self.model)


def _row(
    model: str, input_usd: str, output_usd: str, cache_read: str, cache_write: str
) -> BuiltinPrice:
    return BuiltinPrice(
        model,
        _SINCE,
        Decimal(input_usd),
        Decimal(output_usd),
        Decimal(cache_read),
        Decimal(cache_write),
    )


# 列顺序：模型、输入、输出、缓存读、缓存写（5 分钟）。
BUILTIN_PRICES: tuple[BuiltinPrice, ...] = (
    # Claude
    _row("claude-fable-5-2", "8", "40", "0.20", "10"),
    _row("claude-mythos-5-2", "8", "40", "0.20", "10"),
    _row("claude-fable-5-1", "10", "50", "0.25", "12.50"),
    _row("claude-mythos-5-1", "10", "50", "0.25", "12.50"),
    _row("claude-fable-5", "10", "50", "1", "12.50"),
    _row("claude-mythos-5", "10", "50", "1", "12.50"),
    _row("claude-opus-5-5", "4", "20", "0.20", "5"),
    _row("claude-opus-5", "5", "25", "0.50", "6.25"),
    _row("claude-opus-4-8", "5", "25", "0.50", "6.25"),
    _row("claude-opus-4-7", "5", "25", "0.50", "6.25"),
    _row("claude-opus-4-6", "5", "25", "0.50", "6.25"),
    _row("claude-opus-4-5", "5", "25", "0.50", "6.25"),
    _row("claude-sonnet-5-5", "2", "10", "0.10", "2.50"),
    _row("claude-sonnet-5", "2", "10", "0.20", "2.50"),
    _row("claude-sonnet-4-6", "3", "15", "0.30", "3.75"),
    _row("claude-sonnet-4-5", "3", "15", "0.30", "3.75"),
    _row("claude-haiku-5-5", "0.10", "0.50", "0.01", "0.125"),
    _row("claude-haiku-4-5", "1", "5", "0.10", "1.25"),
    # Codex（OpenAI）
    _row("codex/gpt-6-astra", "10", "50", "1", "0"),
    _row("codex/gpt-6.1-sol", "2", "10", "0.10", "0"),
    _row("codex/gpt-6.1-terra", "1", "5", "0.05", "0"),
    _row("codex/gpt-6-sol", "2", "10", "0.20", "0"),
    _row("codex/gpt-6-luna", "0.10", "0.50", "0.01", "0"),
    _row("codex/gpt-5.6-sol", "4", "20", "0.40", "0"),
    _row("codex/gpt-5.6-terra", "2", "12", "0.20", "0"),
    _row("codex/gpt-5.6-luna", "0.20", "1.20", "0.02", "0"),
    _row("codex/gpt-5.5", "5", "30", "0.50", "0"),
    _row("codex/gpt-5.4", "2.50", "15", "0.25", "0"),
    _row("codex/gpt-5.4-mini", "0.75", "4.50", "0.075", "0"),
    _row("codex/gpt-5.4-nano", "0.20", "1.25", "0.02", "0"),
    _row("codex/gpt-5.3-codex", "1.75", "14", "0.175", "0"),
    _row("codex/gpt-5.2", "1.75", "14", "0.175", "0"),
    _row("codex/gpt-5.1", "1.25", "10", "0.125", "0"),
    _row("codex/gpt-5", "1.25", "10", "0.125", "0"),
    _row("codex/gpt-5-mini", "0.25", "2", "0.025", "0"),
)

# 快照日期后缀（claude-haiku-4-5-20251001、gpt-5.2-2025-12-11）与 `[1m]` 之类的标记。
_SUFFIX = re.compile(r"(?:\[[^\]]*\])+$|-(?:\d{8}|\d{4}-\d{2}-\d{2})$")


def canonical_model(model: str) -> tuple[str, str]:
    """价格匹配用的键：(后端, 去掉路由前缀、快照后缀和标记的模型名)。

    `vllm/claude-opus-4-6`、`claude-opus-4-6[1m]` 与 `claude-opus-4-6` 是同一个模型；
    后端仍按完整名判断，Codex 与 OpenCode 下的同名模型不互相套用价格。
    """
    name = model.strip().rsplit("/", 1)[-1].lower()
    while (stripped := _SUFFIX.sub("", name)) != name:
        name = stripped
    return backend_of(model), name

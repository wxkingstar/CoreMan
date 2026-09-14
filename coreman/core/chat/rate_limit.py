"""限流检测、额度表、预警与候选实例（spec §8.8；使用平台默认文案）。

运行时 撞上 Anthropic 的额度限制时，模型的「回答」就是一句英文告示。这里认出那句话，
把 relay_servers 上定时采集的额度数据拼成一张表接在终稿后面，让用户自己看清哪台还空闲；
`pick_idle_server` 再按团队策略挑一台，由 chat_handler 推一张「切过去吗」的卡片。

百分比与倒计时都只读 `relay_servers` 上的采集列（M3b 由 relay-agent 上报写入，本期只读），
本模块不连库。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from coreman.core.bots.permissions import relay_allowed_for_bot
from coreman.core.bots.relay_policy import relay_visible
from coreman.core.db.models import Bot, RelayServer, User
from coreman.core.i18n.messages import msg

# 中间那个 `·` 是 relay 原样透传的告示里的字符，不是连字符：拿 `-` 去匹配一条都认不出来。
RATE_LIMIT_PATTERN = re.compile(r"You've hit your(?: \w+)? limit · resets")
WARN_5H_PCT = 90
WARN_7D_PCT = 95
# claude 与 minimax 的模型可以互换着跑（同一套 Anthropic 协议），codex 只能落回 codex。
_PROVIDER_SETS = {"claude": {"claude", "minimax"}, "codex": {"codex"}}


def is_rate_limited(text: str) -> bool:
    """这段终稿是不是「撞额度」的告示。"""
    return RATE_LIMIT_PATTERN.search(text) is not None


def allowed_providers(provider: str) -> set[str]:
    """从该 provider 出发，可以切到哪些 provider 的实例上。"""
    return set(_PROVIDER_SETS.get(provider, {provider}))


def _num(pct: Decimal | float | None) -> str:
    """百分比去掉无意义的小数位：90.00 → 90，70.50 → 70.5。"""
    if pct is None:
        return "-"
    value = float(pct)
    return str(int(value)) if value == int(value) else f"{value:g}"


def fmt_pct(pct: Decimal | float | None) -> str:
    if pct is None:
        return "-"
    value = float(pct)
    if value >= 90:
        return f"🔴 {_num(pct)}%"
    if value >= 70:
        return f"🟡 {_num(pct)}%"
    return f"🟢 {_num(pct)}%"


def countdown(resets_at: datetime | None, now: datetime, locale: str = "zh") -> str:
    """距离重置还有多久。采集列里的时刻可能早就过了，按当前时间实时推算。"""
    if resets_at is None:
        return "-"
    left = int((resets_at - now).total_seconds())
    if left <= 0:
        return msg("rl_reset_done", locale)
    hours, minutes = divmod(left // 60, 60)
    return f"{hours}h{minutes}m" if hours else f"{minutes}m"


def ago(probed_at: datetime | None, now: datetime, locale: str = "zh") -> str:
    """这份数据是多久以前采的（15 分钟一轮，用户要知道看的是不是陈数据）。"""
    if probed_at is None:
        return "-"
    seconds = max(0, int((now - probed_at).total_seconds()))
    if seconds < 60:
        return msg("rl_ago_just_now", locale)
    if seconds < 3600:
        return msg("rl_ago_minutes", locale, n=seconds // 60)
    hours, minutes = divmod(seconds // 60, 60)
    return msg("rl_ago_hours", locale, h=hours, m=minutes)


def quota_table(
    relays: Sequence[RelayServer],
    *,
    current_id: uuid.UUID | None,
    now: datetime,
    locale: str = "zh",
) -> str:
    """全部在用实例的额度总览；每台两行，第二行是采集时间与两个倒计时。停用的不列。"""
    lines = [msg("rl_quota_title", locale)]
    lines.append(msg("rl_quota_head", locale))
    # 分隔行只有管道与横线，没有可翻译的字：markdown 不校验宽度，两种语言共用一行。
    lines.append("|------|------|-----------|---------|")
    for r in relays:
        if not r.is_active:
            continue
        name = f"{r.name} ⭐" if r.id == current_id else r.name
        lines.append(
            f"| {name} | {r.model_provider} "
            f"| {fmt_pct(r.rate_limit_5h_used_pct)} | {fmt_pct(r.rate_limit_7d_used_pct)} |"
        )
        probed = msg("rl_quota_probed", locale, ago=ago(r.rate_limit_probed_at, now, locale))
        lines.append(
            f"| {probed} | "
            f"| ↳{countdown(r.rate_limit_5h_resets_at, now, locale)} "
            f"| ↳{countdown(r.rate_limit_7d_resets_at, now, locale)} |"
        )
    lines.append("")
    lines.append(msg("rl_quota_footer", locale))
    return "\n".join(lines)


def quota_warning(current: RelayServer, now: datetime, locale: str = "zh") -> str:
    """还没撞限但快了：5 小时 ≥90% 或 7 天 ≥95% 时提前告知。都没到就返回空串。"""
    p5, p7 = current.rate_limit_5h_used_pct, current.rate_limit_7d_used_pct
    hot5 = p5 is not None and float(p5) >= WARN_5H_PCT
    hot7 = p7 is not None and float(p7) >= WARN_7D_PCT
    if not hot5 and not hot7:
        return ""
    note = msg("rl_probed_note", locale, ago=ago(current.rate_limit_probed_at, now, locale))
    reset5 = countdown(current.rate_limit_5h_resets_at, now, locale)
    reset7 = countdown(current.rate_limit_7d_resets_at, now, locale)
    # 采集里没带重置时刻时写「稍后」：一句「约 - 后重置」读起来像坏了。
    reset5 = reset5 if reset5 != "-" else msg("rl_later", locale)
    reset7 = reset7 if reset7 != "-" else msg("rl_later", locale)
    if hot5 and hot7:
        return msg(
            "rl_warn_both",
            locale,
            note=note,
            p5=_num(p5),
            reset5=reset5,
            p7=_num(p7),
            reset7=reset7,
        )
    if hot5:
        return msg("rl_warn_5h", locale, pct=_num(p5), note=note, reset=reset5)
    return msg("rl_warn_7d", locale, pct=_num(p7), note=note, reset=reset7)


def pick_idle_server(
    relays: Sequence[RelayServer],
    *,
    current: RelayServer,
    bot: Bot,
    user: User,
    creator_team_id: uuid.UUID | None,
) -> RelayServer | None:
    """挑一台能接手的实例：同族 provider、在用、有采集数据、两道策略都过，取 7 天最空的。

    没有采集数据的一律不推荐——「不知道空不空」不是「空」，切过去可能撞得更快。
    可见性与团队策略都要查，与 `switch_relay` 执行时一致：只查团队策略的话，
    `visibility='admins'` 的实例会被推荐给非 manager，用户点「切换」只换来一条 422。
    """
    providers = allowed_providers(current.model_provider)
    candidates = [
        r
        for r in relays
        if r.is_active
        and r.id != current.id
        and r.model_provider in providers
        and r.rate_limit_7d_used_pct is not None
        and relay_visible(user, r)
        and relay_allowed_for_bot(user, r, bot_team_id=bot.team_id, creator_team_id=creator_team_id)
    ]
    if not candidates:
        return None
    # 名字做次序兜底：并列时每次都挑同一台，用户不会在两次撞限里被推去两个不同的地方。
    return min(candidates, key=lambda r: (float(r.rate_limit_7d_used_pct or 0), r.name))

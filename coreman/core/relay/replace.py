"""模型换代：新版本顶替目录里的旧版本，在用的 AI 员工与白名单一起换过去。

一次替换做这些事（同一事务）：
- 新模型不在目录里就新增（排在被替代模型中最靠前的位置）；在目录里但已退役就取消退役。
- 旧模型全部退役；其中有默认模型，或调用方要求，新模型成为该提供方的默认。
- 用旧模型的 AI 员工改用新模型；新模型不支持原来的思考档位时降到它支持的最高档。
- 白名单（restricted）运行时里的旧模型换成新模型，占第一个旧模型的位置，重复项去掉。

`dry_run` 只算出会改什么，不写库。价格另由 `model_prices` 管理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.events import notify_bot_changed
from coreman.core.db.models import Bot, ModelCatalog, RelayServer
from coreman.core.relay.models import default_model, fit_effort, known_effort_flags, load_catalog


class ReplaceError(ValueError):
    """请求本身不成立（模型不存在、新旧相同等），由调用方转成 4xx。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class ReplacePlan:
    provider: str
    new: str
    old: list[str]
    created: bool
    default_before: str | None
    default_after: str | None
    bots: list[dict[str, Any]] = field(default_factory=list)
    relays: list[dict[str, Any]] = field(default_factory=list)

    def out(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "new": self.new,
            "old": self.old,
            "created": self.created,
            "default_before": self.default_before,
            "default_after": self.default_after,
            "bots": self.bots,
            "relays": self.relays,
        }


async def replace_models(
    session: AsyncSession,
    *,
    provider: str,
    new: str,
    old: list[str],
    display_name: str | None = None,
    supports_xhigh: bool | None = None,
    supports_max: bool | None = None,
    make_default: bool = False,
    dry_run: bool = False,
) -> ReplacePlan:
    old = list(dict.fromkeys(old))
    if new in old:
        raise ReplaceError(422, "新模型不能同时是被替代的模型")
    catalog = await load_catalog(session, provider)
    rows = {r.model: r for r in catalog}
    missing = [m for m in old if m not in rows]
    if missing:
        raise ReplaceError(404, f"目录里没有这些模型：{'、'.join(missing)}")

    default_before = default_model(provider, catalog)
    existing = rows.get(new)
    flags = known_effort_flags(new)
    if existing is not None:
        flags = {"supports_xhigh": existing.supports_xhigh, "supports_max": existing.supports_max}
    if supports_xhigh is not None:
        flags["supports_xhigh"] = supports_xhigh
    if supports_max is not None:
        flags["supports_max"] = supports_max
    # 替换后新模型那一行的样子（不挂到 session 上，预览不会写库），用来算思考档位。
    planned = ModelCatalog(provider=provider, model=new, **flags)
    after = [planned, *(r for r in catalog if r.model != new)]
    becomes_default = make_default or any(rows[m].is_default for m in old)

    bots = (
        await session.execute(
            select(Bot)
            .outerjoin(RelayServer, RelayServer.id == Bot.relay_server_id)
            .where(
                Bot.model.in_(old),
                # bots 没有 provider 列，由所绑运行时决定；同名模型可能挂在多个 provider 下。
                or_(Bot.relay_server_id.is_(None), RelayServer.model_provider == provider),
            )
            .order_by(Bot.name)
        )
    ).scalars()
    bot_changes: list[tuple[Bot, str | None]] = []
    bot_out: list[dict[str, Any]] = []
    for bot in bots:
        effort = bot.effort_level
        new_effort = fit_effort(new, effort, after) if effort else None
        bot_changes.append((bot, new_effort))
        bot_out.append(
            {
                "id": str(bot.id),
                "name": bot.name,
                "model": bot.model,
                "effort_level": effort,
                "new_effort_level": new_effort,
            }
        )

    relays = (
        await session.execute(
            select(RelayServer)
            .where(
                RelayServer.model_provider == provider,
                RelayServer.supported_models_mode == "restricted",
                RelayServer.supported_models.overlap(old),
            )
            .order_by(RelayServer.name)
        )
    ).scalars()
    relay_changes: list[tuple[RelayServer, list[str]]] = []
    relay_out: list[dict[str, Any]] = []
    for relay in relays:
        before = list(relay.supported_models or [])
        updated = list(dict.fromkeys(new if m in old else m for m in before))
        relay_changes.append((relay, updated))
        relay_out.append(
            {"id": str(relay.id), "name": relay.name, "before": before, "after": updated}
        )

    plan = ReplacePlan(
        provider=provider,
        new=new,
        old=old,
        created=existing is None,
        default_before=default_before,
        default_after=new if becomes_default else default_before,
        bots=bot_out,
        relays=relay_out,
    )
    if dry_run:
        return plan

    if existing is None:
        target = ModelCatalog(
            provider=provider,
            model=new,
            display_name=display_name,
            is_default=False,
            retired=False,
            sort_order=max(rows[m].sort_order for m in old),
            **flags,
        )
        session.add(target)
    else:
        target = existing
        target.retired = False
        target.supports_xhigh = flags["supports_xhigh"]
        target.supports_max = flags["supports_max"]
        if display_name is not None:
            target.display_name = display_name
    for m in old:
        rows[m].retired = True
        rows[m].is_default = False
    if becomes_default:
        for r in [target, *(r for r in catalog if r is not target)]:
            r.is_default = r is target
    for bot, new_effort in bot_changes:
        bot.model = new
        bot.effort_level = new_effort
    for relay, updated in relay_changes:
        relay.supported_models = updated
    await session.flush()
    for bot, _ in bot_changes:
        await notify_bot_changed(session, bot.id)
    return plan

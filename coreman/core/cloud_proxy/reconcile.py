"""对账：回调送不到时（CoreMan 停机、网络断了超过 Tower 的重试窗口），翻 Tower 的状态接口补上决定。

决定按回调同一套逻辑落地（decisions.apply），所以重复处理、已执行的单、认不出的状态都是无事发生。

执行方式：scheduler 主实例每 5 分钟自动跑一轮（TOWER_URL 与 TOWER_SENDER_KEY 都配了才跑）；
手动补跑用 `python -m coreman.cli tower reconcile`，输出本次翻页统计 JSON。
"""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.cloud_proxy import decisions, resume
from coreman.core.logging import get_logger
from coreman.core.settings_store import SettingsStore
from coreman.core.timeutils import parse_ts

log = get_logger(__name__)
CURSOR_KEY = "tower.reconcile_cursor"
PAGE_LIMIT = 100
TIMEOUT_SECONDS = 10.0
# 与 service.py 生成 external_id 的前缀一致：别的流程提交的审批不归这里管
EXTERNAL_PREFIX = "cm-"
# 防 Tower 异常时游标一直在变导致死循环；每页 100 条，足够追平任何合理积压
MAX_PAGES = 500


class ReconcileError(Exception):
    """Tower 没按约定应答；消息不含密钥。"""


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=TIMEOUT_SECONDS)


async def fetch_page(
    http: httpx.AsyncClient, tower_url: str, key: str, cursor: str
) -> tuple[list[Any], str]:
    try:
        resp = await http.get(
            tower_url.rstrip("/") + "/v1/approvals",
            params={"updated_since": cursor, "limit": PAGE_LIMIT},
            headers={"X-API-Key": key},
        )
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ReconcileError(f"Tower 请求失败：{type(exc).__name__}") from None
    if resp.status_code != 200 or not isinstance(data, dict):
        raise ReconcileError(f"Tower 应答异常：HTTP {resp.status_code}")
    items, nxt = data.get("items"), data.get("next_cursor")
    if not isinstance(items, list) or not isinstance(nxt, str):
        raise ReconcileError("Tower 应答格式不对")
    return items, nxt


async def apply_item(session: AsyncSession, item: Any) -> bool:
    """返回是否有挂起单因此迁移；不合格的条目一律跳过。"""
    if not isinstance(item, dict):
        return False
    approval_id, external_id, status = (
        item.get("approval_id"),
        item.get("external_id"),
        item.get("status"),
    )
    if (
        not isinstance(approval_id, str)
        or not decisions.APPROVAL_ID.fullmatch(approval_id)
        or not isinstance(external_id, str)
        or not external_id.startswith(EXTERNAL_PREFIX)
        or len(external_id) > 256
        or status not in resume.RESUME_STATUSES
    ):
        return False
    reason = item.get("reason")
    outcome = await decisions.apply(
        session,
        approval_id=approval_id,
        external_id=external_id,
        status=status,
        reason=reason if isinstance(reason, str) else None,
        decided_at=parse_ts(item.get("decided_at")),
    )
    return outcome.known


async def run(
    factory: async_sessionmaker[AsyncSession],
    store: SettingsStore,
    *,
    tower_url: str,
    sender_key: str,
    http: httpx.AsyncClient | None = None,
) -> dict[str, int]:
    """从游标起翻到追平；每页落地并提交之后才推进游标，中途失败下次从该页重来（落地幂等）。"""
    owned = http is None
    http = http or client()
    cursor = await store.get(CURSOR_KEY, "")
    if not isinstance(cursor, str):
        cursor = ""
    stats = {"pages": 0, "seen": 0, "matched": 0, "failed": 0}
    try:
        for _ in range(MAX_PAGES):
            items, nxt = await fetch_page(http, tower_url, sender_key, cursor)
            for item in items:
                async with factory() as session:
                    try:
                        # 一条出错只丢这一条：否则它会卡住游标，后面的决定都对不上
                        async with session.begin_nested():
                            if await apply_item(session, item):
                                stats["matched"] += 1
                    except Exception as exc:  # noqa: BLE001
                        ext = item.get("external_id") if isinstance(item, dict) else None
                        log.error(
                            "tower_reconcile_item_failed",
                            external_id=ext,
                            error=type(exc).__name__,
                        )
                        stats["failed"] += 1
                    await session.commit()
            stats["pages"] += 1
            stats["seen"] += len(items)
            if nxt and nxt != cursor:
                await store.set(CURSOR_KEY, nxt)
                cursor = nxt
            if len(items) < PAGE_LIMIT:
                break
    finally:
        if owned:
            await http.aclose()
    return stats

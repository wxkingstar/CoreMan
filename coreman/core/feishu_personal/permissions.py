"""Application-enabled user OAuth scopes and local authorization tier selection."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

LEVELS = ("messages_readonly", "all_except_send", "all")
MESSAGE_SCOPES = frozenset(
    {
        "offline_access",
        "auth:user.id:read",
        "search:message",
        "im:message:readonly",
        "im:message.group_msg:get_as_user",
        "im:message.p2p_msg:get_as_user",
        "im:chat:read",
    }
)
BASE = "https://open.feishu.cn/open-apis"
_SCOPE = re.compile(r"[a-z0-9_.]+(?::[a-z0-9_.]+)+\Z")
_SEND = re.compile(r"(?:^|[.:_])(send|reply|forward)(?:$|[.:_])")
# Named like sending but only answers the owner's own invitation (accept, decline, maybe).
NOT_SENDING = frozenset({"calendar:calendar.event:reply"})


class PermissionsError(ValueError):
    """Sanitized discovery failure; never include upstream secrets or payloads."""

    code = "app_scopes_unavailable"

    def __init__(self, code: str = "app_scopes_unavailable") -> None:
        self.code = code
        super().__init__(self.code)


def select_scopes(level: str, available: list[str]) -> list[str]:
    """Select only discovered scopes; execution must also enforce the saved tier.

    Broad IM message grants combine read and send, so they cannot be requested
    for the middle tier. Other write/delete/group permissions remain available.
    OAuth grants alone cannot prevent sending via a future composite permission:
    the service must independently prohibit send/reply/forward operations.
    """
    if level not in LEVELS:
        raise ValueError("invalid_permission_level")
    scopes = set(available)
    if any(
        not isinstance(s, str) or not (s == "offline_access" or _SCOPE.fullmatch(s)) for s in scopes
    ):
        raise PermissionsError()
    if level == "messages_readonly":
        scopes &= MESSAGE_SCOPES
    elif level == "all_except_send":
        scopes = {
            s for s in scopes if s != "im:message" and (s in NOT_SENDING or not _SEND.search(s))
        }
    return sorted(scopes)


async def app_user_scopes(
    app_id: str,
    secret: str,
    *,
    http: Callable[..., Awaitable[dict[str, Any]]],
) -> list[str]:
    """Fetch this app's enabled user grants, never the platform-wide catalog.

    Matches official larksuite/cli cmd/auth/auth.go getAppInfo: application v6
    detail authenticated as bot, filtering data.app.scopes by token_types=user.
    The HTTP dependency uses service._http's signature and redirect protections.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", app_id) or not secret:
        raise PermissionsError()
    token = await http(
        "POST",
        BASE + "/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": secret},
    )
    if (
        token.get("code") != 0
        or not isinstance(token.get("tenant_access_token"), str)
        or not token["tenant_access_token"]
    ):
        raise PermissionsError()
    result = await http(
        "GET",
        BASE + "/application/v6/applications/" + app_id,
        params={"lang": "zh_cn"},
        headers={"Authorization": "Bearer " + token["tenant_access_token"]},
    )
    if result.get("code") == 99991672:
        raise PermissionsError("app_scope_discovery_permission_missing")
    if result.get("code") != 0:
        raise PermissionsError()
    data = result.get("data")
    app = data.get("app") if isinstance(data, dict) else None
    scopes = app.get("scopes") if isinstance(app, dict) else None
    if not isinstance(scopes, list):
        raise PermissionsError()
    user_scopes = []
    for item in scopes:
        if not isinstance(item, dict) or not isinstance(item.get("token_types"), list):
            raise PermissionsError()
        if "user" not in item["token_types"]:
            continue
        scope = item.get("scope")
        if not isinstance(scope, str) or not (scope == "offline_access" or _SCOPE.fullmatch(scope)):
            raise PermissionsError()
        user_scopes.append(scope)
    # offline_access is a protocol grant, not an application permission. Official
    # device_flow.go adds it to every request to permit renewable authorization.
    return sorted(set(user_scopes) | {"offline_access"})

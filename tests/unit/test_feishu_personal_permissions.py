from unittest.mock import AsyncMock

import pytest

from coreman.core.feishu_personal import permissions
from coreman.core.feishu_personal.permissions import (
    MESSAGE_SCOPES,
    PermissionsError,
    app_user_scopes,
    select_scopes,
)


def test_message_tier_never_adds_undiscovered_application_permissions():
    available = ["offline_access", "im:chat:read", "docx:document:readonly", "im:message"]
    assert select_scopes("messages_readonly", available) == ["im:chat:read", "offline_access"]
    assert "docx:document:readonly" not in MESSAGE_SCOPES


def test_middle_tier_preserves_writes_but_excludes_all_message_sending_forms():
    writes = [
        "docx:document",
        "im:chat:update",
        "im:message:delete",
        "calendar:calendar.event:create",
        # Answering one's own invitation is not sending, despite the name.
        "calendar:calendar.event:reply",
    ]
    sends = [
        "im:message",
        "im:message.send_as_user",
        "im:message:send_as_bot",
        "im:message:reply",
        "im:message:forward",
        "mail:user_mailbox.message:send",
    ]
    assert select_scopes("all_except_send", writes + sends) == sorted(writes)
    assert select_scopes("all", writes + sends + writes) == sorted(set(writes + sends))


def test_no_messages_tier_is_the_middle_tier_without_any_chat_permission():
    kept = ["docx:document", "calendar:calendar.event:create", "calendar:calendar.event:reply"]
    dropped = [
        "search:message",
        "im:message:readonly",
        "im:message.p2p_msg:get_as_user",
        "im:chat:read",
        "im:chat:create_by_user",
        "im:message.send_as_user",
        "mail:user_mailbox.message:send",
    ]
    assert select_scopes("no_messages", kept + dropped + ["offline_access"]) == sorted(
        kept + ["offline_access"]
    )


def test_invalid_tier_and_corrupt_scope_fail_closed():
    with pytest.raises(ValueError, match="invalid_permission_level"):
        select_scopes("unknown", [])
    with pytest.raises(PermissionsError):
        select_scopes("all", ["im:chat:read im:message"])


@pytest.mark.asyncio
async def test_discovery_uses_selected_app_and_filters_tenant_scopes():
    http = AsyncMock(
        side_effect=[
            {"code": 0, "tenant_access_token": "tenant-token"},
            {
                "code": 0,
                "data": {
                    "app": {
                        "scopes": [
                            {"scope": "im:message.send_as_user", "token_types": ["user"]},
                            {"scope": "docx:document", "token_types": ["tenant", "user"]},
                            {"scope": "im:message:send_as_bot", "token_types": ["tenant"]},
                        ]
                    }
                },
            },
        ]
    )
    assert await app_user_scopes("cli_selected", "secret", http=http) == [
        "docx:document",
        "im:message.send_as_user",
        "offline_access",
    ]
    first, second = http.call_args_list
    assert first.kwargs["json"] == {"app_id": "cli_selected", "app_secret": "secret"}
    assert second.args == (
        "GET",
        "https://open.feishu.cn/open-apis/application/v6/applications/cli_selected",
    )
    assert second.kwargs["headers"] == {"Authorization": "Bearer tenant-token"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"code": 1, "msg": "sensitive upstream text"},
        {"code": 0, "data": {}},
        {"code": 0, "data": {"app": {"scopes": [{"scope": "im:chat:read"}]}}},
    ],
)
async def test_discovery_failures_never_fall_back_to_global_permissions(body):
    http = AsyncMock(side_effect=[{"code": 0, "tenant_access_token": "t"}, body])
    with pytest.raises(PermissionsError, match="^app_scopes_unavailable$"):
        await app_user_scopes("cli_app", "secret", http=http)


@pytest.mark.asyncio
async def test_invalid_app_path_is_rejected_without_http():
    http = AsyncMock()
    with pytest.raises(PermissionsError):
        await app_user_scopes("../other", "secret", http=http)
    http.assert_not_called()


@pytest.mark.asyncio
async def test_discovery_reports_missing_app_introspection_permission_without_leaking_payload():
    http = AsyncMock(
        side_effect=[
            {"code": 0, "tenant_access_token": "t"},
            {"code": 99991672, "msg": "sensitive app link and metadata"},
        ]
    )
    with pytest.raises(PermissionsError) as error:
        await app_user_scopes("cli_app", "secret", http=http)
    assert error.value.code == "app_scope_discovery_permission_missing"
    assert str(error.value) == "app_scope_discovery_permission_missing"


def test_message_range_drops_the_other_kind_of_chat() -> None:
    scopes = sorted(permissions.MESSAGE_SCOPES)
    group = permissions.narrow(scopes, "group")
    assert permissions.GROUP_READ in group and permissions.P2P_READ not in group
    assert "im:message:readonly" in group
    p2p = permissions.narrow(scopes, "p2p")
    assert permissions.P2P_READ in p2p and permissions.GROUP_READ not in p2p
    assert permissions.narrow(scopes, "all") == scopes
    with pytest.raises(ValueError):
        permissions.narrow(scopes, "some")


def test_range_is_enforced_only_when_feishu_left_the_other_kind_out() -> None:
    assert permissions.range_enforced("group", ["im:message:readonly", permissions.GROUP_READ])
    # 本人以前同意过单聊那一项：飞书照样发下来，只靠平台拦。
    assert not permissions.range_enforced("group", [permissions.GROUP_READ, permissions.P2P_READ])
    assert permissions.range_enforced("all", [permissions.P2P_READ])

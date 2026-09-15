"""已删除的旧别名路由不再存在；仍有技能调用的别名保留（未签名时是 401 而不是 404）。"""

import pytest

REMOVED = [
    ("POST", "/api/robot/memories/collect"),
    ("GET", "/api/robot/memories/query"),
    ("POST", "/api/robot/memories/deploy"),
    ("POST", "/api/robot/wework-notify"),
    ("GET", "/api/robot/organization/tree"),
    ("GET", "/api/organization/full"),
    ("POST", "/api/push"),
    ("POST", "/api/test/bot-token-access"),
]

# human-escalation 技能（escalation_cli.py）仍调用这些路径。
RETAINED = [
    ("POST", "/api/escalation/create"),
    ("GET", "/api/escalation/group-x/poll"),
    ("POST", "/api/escalation/group-x/resolve"),
    ("POST", "/api/escalation/group-x/followup"),
    ("POST", "/api/escalation/group-x/cancel"),
    ("GET", "/api/robot/organization/members"),
]


@pytest.mark.parametrize("method,path", REMOVED)
async def test_removed_aliases_return_not_found(client, method: str, path: str) -> None:
    response = await client.request(method, path, json={})
    assert response.status_code in {404, 405}


@pytest.mark.parametrize("method,path", RETAINED)
async def test_retained_aliases_still_require_signature(client, method: str, path: str) -> None:
    response = await client.request(method, path, json={})
    assert response.status_code == 401, response.text

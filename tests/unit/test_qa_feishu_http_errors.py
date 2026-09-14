"""HTTP error bodies must retain safe codes for refresh and failure classification."""
import httpx
import pytest

from coreman.core.platforms.feishu import FeishuClient, FeishuError


async def test_expired_token_on_http_error_refreshes_once():
    tokens = []
    calls = []

    def respond(request):
        if request.url.path.endswith('/tenant_access_token/internal'):
            token = f'test-token-{len(tokens)}'
            tokens.append(token)
            return httpx.Response(200, json={
                'code': 0, 'tenant_access_token': token, 'expire': 7200,
            })
        calls.append(request.headers['Authorization'])
        if len(calls) == 1:
            return httpx.Response(401, json={'code': 99991663, 'msg': 'private-detail'})
        return httpx.Response(200, json={'code': 0, 'data': {'ok': True}})

    async with httpx.AsyncClient(
        base_url='https://open.feishu.cn', transport=httpx.MockTransport(respond),
    ) as http:
        client = FeishuClient('test-app', 'test-secret', http=http)
        assert (await client.call('GET', '/example'))['data']['ok']
        assert calls == ['Bearer test-token-0', 'Bearer test-token-1']
        assert len(tokens) == 2


@pytest.mark.parametrize('status,body,expected', [
    (400, {'code': 230013, 'msg': 'private-detail'}, 230013),
    (503, {'code': 0, 'msg': 'private-detail'}, 503),
    (502, 'private-detail', 502),
])
async def test_http_error_code_without_exposing_body(status, body, expected):
    def respond(request):
        if isinstance(body, dict):
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=body)

    async with httpx.AsyncClient(
        base_url='https://open.feishu.cn', transport=httpx.MockTransport(respond),
    ) as http:
        client = FeishuClient('test-app', 'test-secret', http=http)
        with pytest.raises(FeishuError) as error:
            await client.call('GET', '/example', token='explicit-test-token')
        assert error.value.code == expected
        assert 'private-detail' not in str(error.value)

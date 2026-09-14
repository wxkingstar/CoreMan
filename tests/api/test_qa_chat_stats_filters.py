"""Regression: visible filters must apply equally to chat list and statistics."""
import pytest

from tests.api.conftest import login_existing
from tests.api.test_chat_logs import _seed


@pytest.mark.parametrize('filters,total', [
    ({'keyword': '回答3'}, 1),
    ({'keyword': '回答2'}, 0),  # another user's inaccessible conversation
    ({'keyword': '%'}, 0),
    ({'status': 'error'}, 1),
    ({'chat_type': 'group'}, 0),
    ({'user': 'crea'}, 2),
    ({'user': 'crea', 'status': 'stopped', 'keyword': '回答3'}, 1),
])
async def test_filtered_statistics_match_visible_records(client, db_session, filters, total):
    _, _, creator = await _seed(db_session)
    await login_existing(client, db_session, creator)
    listing = (await client.get('/api/admin/chat-logs', params=filters)).json()['data']
    statistics = (await client.get('/api/admin/chat-logs/stats', params=filters)).json()['data']
    assert listing['total'] == total
    assert statistics['total'] == total
    assert sum(statistics['by_status'].values()) == total
    assert sum(row['total'] for row in statistics['by_bot']) == total
    assert statistics['tokens']['input'] == 10 * total

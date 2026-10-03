"""Unit tests for the HTTP API send and status endpoints."""

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy.exc import SQLAlchemyError

from porth.config.settings import RoutingConfig, Settings
from porth.core.message import MessageStatus
from porth.core.queue import MessageQueue
from porth.core.routing import Router
from porth.protocols.http.api import create_http_app

BODY = {
    'source_addr': 'porth',
    'destination_addr': '+306900000000',
    'message_text': 'hi',
}


class RecordingQueue(MessageQueue):
    """Records, per put, whether the message's unit of work had committed."""

    def __init__(self, uow_factory):
        super().__init__()
        self.uow_factory = uow_factory
        self.committed_at_put: list[bool] = []

    async def put(self, message):
        self.committed_at_put.append(self.uow_factory.uows[-1].committed)
        await super().put(message)


# Two SMSCs: 30... goes out through a, 44... through b; nothing else routes
ROUTER = Router(('a', 'b'), RoutingConfig(prefixes={'30': 'a', '44': 'b'}))


@pytest_asyncio.fixture
async def queues(uow_factory):
    return {'a': RecordingQueue(uow_factory), 'b': RecordingQueue(uow_factory)}


@pytest_asyncio.fixture
async def http(uow_factory, queues):
    app = create_http_app(queues, ROUTER, uow_factory, Settings())
    async with TestClient(TestServer(app)) as client:
        yield client, queues['a'], uow_factory.repo


async def submit(client) -> str:
    resp = await client.post('/api/v1/sms/send', json=BODY)
    assert resp.status == 200
    return (await resp.json())['message_id']


@pytest.mark.asyncio
async def test_poll_after_submit_is_queued(http):
    client, queue, _ = http
    message_id = await submit(client)
    resp = await client.get(f'/api/v1/sms/status/{message_id}')
    assert resp.status == 200
    body = await resp.json()
    assert body['message_id'] == message_id
    assert body['status'] == 'queued'
    assert datetime.strptime(body['created_at'], '%Y-%m-%dT%H:%M:%SZ')
    assert body['sent_at'] is None
    assert body['delivered_at'] is None
    assert (await queue.get()).dlr_requested is True


@pytest.mark.asyncio
async def test_submit_commits_queued_before_it_is_put(http):
    client, queue, store = http
    message_id = await submit(client)
    assert queue.committed_at_put == [True]
    assert store.messages[message_id].status == MessageStatus.QUEUED


@pytest.mark.asyncio
async def test_failed_commit_is_503_and_queues_nothing(http, uow_factory):
    client, queue, _ = http
    uow_factory.fail = SQLAlchemyError('database down')
    resp = await client.post('/api/v1/sms/send', json=BODY)
    assert resp.status == 503
    assert (await resp.json())['error'] == 'Failed to send SMS'
    assert queue.empty()


@pytest.mark.asyncio
async def test_poll_shows_status_changes_of_the_stored_message(http):
    client, _, store = http
    message_id = await submit(client)
    message = store.messages[message_id]
    message.status = MessageStatus.SENT
    message.sent_at = datetime(2026, 9, 26, 12, 30, 5, tzinfo=timezone.utc)
    body = await (await client.get(f'/api/v1/sms/status/{message_id}')).json()
    assert body['status'] == 'sent'
    assert body['sent_at'] == '2026-09-26T12:30:05Z'


@pytest.mark.asyncio
async def test_unknown_id_is_404(http):
    client, _, _ = http
    resp = await client.get('/api/v1/sms/status/nope')
    assert resp.status == 404
    assert await resp.json() == {'error': 'Message not found', 'details': 'nope'}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'field, value',
    [
        ('dlr_url', 'http://example.com/dlr'),
        ('dlr_url', None),
        ('dlr_url', ''),
        ('colour', 'red'),
    ],
)
async def test_unknown_field_is_rejected(http, field, value):
    client, queue, store = http
    resp = await client.post('/api/v1/sms/send', json={**BODY, field: value})
    assert resp.status == 400
    assert (await resp.json())['details'] == f"unknown field(s): ['{field}']"
    assert queue.empty()
    assert store.messages == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('sent, kept', [('high', 'high'), (None, 'normal')])
async def test_priority_is_stored(http, sent, kept):
    client, _, store = http
    body = BODY if sent is None else {**BODY, 'priority': sent}
    resp = await client.post('/api/v1/sms/send', json=body)
    assert resp.status == 200
    assert store.messages[(await resp.json())['message_id']].priority == kept


@pytest.mark.asyncio
@pytest.mark.parametrize('priority', ['urgent', 1])
async def test_bad_priority_is_rejected(http, priority):
    client, queue, store = http
    resp = await client.post('/api/v1/sms/send', json={**BODY, 'priority': priority})
    assert resp.status == 400
    assert 'priority' in (await resp.json())['details']
    assert queue.empty()
    assert store.messages == {}


@pytest.mark.asyncio
async def test_submit_is_routed_to_its_smscs_queue(http, queues):
    client, queue, store = http
    resp = await client.post(
        '/api/v1/sms/send', json={**BODY, 'destination_addr': '+447000000001'}
    )
    assert resp.status == 200
    message_id = (await resp.json())['message_id']
    assert queue.empty()
    assert (await queues['b'].get()).smsc == 'b'
    assert store.messages[message_id].smsc == 'b'


@pytest.mark.asyncio
async def test_unroutable_number_is_400_and_stores_nothing(http, queues):
    client, _, store = http
    resp = await client.post(
        '/api/v1/sms/send', json={**BODY, 'destination_addr': '15550000001'}
    )
    assert resp.status == 400
    assert await resp.json() == {
        'error': 'Failed to send SMS',
        'details': "no route for '15550000001'",
    }
    assert all(q.empty() for q in queues.values())
    assert store.messages == {}


@pytest.mark.asyncio
async def test_health_sums_the_queues(http):
    client, _, _ = http
    for to in ('+306900000001', '+447000000001', '+447000000002'):
        resp = await client.post(
            '/api/v1/sms/send', json={**BODY, 'destination_addr': to}
        )
        assert resp.status == 200
    assert (await (await client.get('/health')).json())['queue_size'] == 3


@pytest.mark.asyncio
async def test_callback_url_is_stored_as_is(http):
    client, _, store = http
    url = 'https://messgr.example/cb/t%41k?x=%d'
    resp = await client.post('/api/v1/sms/send', json={**BODY, 'callback_url': url})
    assert resp.status == 200
    assert store.messages[(await resp.json())['message_id']].callback_url == url


@pytest.mark.asyncio
@pytest.mark.parametrize('url', ['ftp://x', 'x', 5, 'http://', 'http://['])
async def test_bad_callback_url_is_rejected(http, url):
    client, queue, store = http
    resp = await client.post('/api/v1/sms/send', json={**BODY, 'callback_url': url})
    assert resp.status == 400
    assert not store.messages and queue.empty()


@pytest.mark.asyncio
async def test_valid_until_and_keep_text_are_stored(http):
    client, _, store = http
    body = {**BODY, 'valid_until': '2026-10-01T12:00:00+02:00', 'keep_text': False}
    resp = await client.post('/api/v1/sms/send', json=body)
    assert resp.status == 200
    message = store.messages[(await resp.json())['message_id']]
    assert message.valid_until == datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
    assert message.valid_until.utcoffset().total_seconds() == 0  # stored in UTC
    assert message.keep_text is False


@pytest.mark.asyncio
async def test_validity_and_keep_text_default_to_none_and_kept(http):
    client, _, store = http
    message = store.messages[await submit(client)]
    assert (message.valid_until, message.keep_text) == (None, True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'field, value',
    [
        ('valid_until', '2026-10-01T10:00:00'),  # naive
        ('valid_until', 'soon'),
        ('valid_until', 5),
        ('valid_until', '9999-12-31T23:59:59-05:00'),  # past the last UTC datetime
        ('valid_until', '0001-01-01T00:00:00+14:00'),  # before the first
        ('valid_until', '0001-01-01T00:00:00Z'),  # PostgreSQL's -infinity
        ('valid_until', '1999-12-31T23:59:59Z'),
        ('valid_until', '2100-01-01T00:00:00Z'),  # submit_sm's year wraps to 2000
        ('valid_until', '2099-12-31T23:00:00-05:00'),  # 2100 in UTC
        ('keep_text', 'no'),
        ('keep_text', 0),
    ],
)
async def test_bad_validity_or_keep_text_is_rejected(http, field, value):
    client, queue, store = http
    resp = await client.post('/api/v1/sms/send', json={**BODY, field: value})
    assert resp.status == 400
    assert field in (await resp.json())['details']
    assert queue.empty()
    assert store.messages == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('sent', ['2000-01-01T00:00:00Z', '2099-12-31T23:59:59Z'])
async def test_valid_until_at_either_end_of_the_range_is_taken(http, sent):
    client, _, store = http
    resp = await client.post('/api/v1/sms/send', json={**BODY, 'valid_until': sent})
    assert resp.status == 200
    stored = store.messages[(await resp.json())['message_id']].valid_until
    assert stored == datetime.fromisoformat(sent)

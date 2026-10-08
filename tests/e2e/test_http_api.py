"""Unit tests for the HTTP API send and status endpoints."""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy.exc import SQLAlchemyError

from porth.config.settings import RoutingConfig, Settings
from porth.domain.model import MessageStatus
from porth.adapters.queue import MessageQueue
from porth.service_layer.routing import Router
from porth.entrypoints.http_api import create_http_app
from tests.conftest import sample

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


def smsc_state(bound_a=True):
    return {
        'a': {'bound': bound_a, 'waiting': 2, 'retrying': 1},
        'b': {'bound': True, 'waiting': 0, 'retrying': 0},
    }


@pytest_asyncio.fixture
async def http(uow_factory, queues):
    app = create_http_app(queues, ROUTER, uow_factory, Settings(), smsc_state)
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


@pytest.mark.asyncio
async def test_repeat_key_answers_the_first_message_and_queues_nothing(http):
    client, queue, store = http
    keyed = {**BODY, 'idempotency_key': 'k-1'}
    first = await (await client.post('/api/v1/sms/send', json=keyed)).json()
    store.messages[first['message_id']].status = MessageStatus.SENT
    resp = await client.post(
        '/api/v1/sms/send', json={**keyed, 'destination_addr': '+306911111111'}
    )
    assert resp.status == 200
    assert await resp.json() == {
        'message_id': first['message_id'],
        'status': 'sent',
        'message': 'Message already accepted',
    }
    assert queue.qsize() == 1
    assert len(store.messages) == 1
    assert store.messages[first['message_id']].idempotency_key == 'k-1'


@pytest.mark.asyncio
async def test_repeat_key_is_answered_even_when_it_no_longer_routes(http):
    client, _, store = http
    keyed = {**BODY, 'idempotency_key': 'k-1'}
    first = await (await client.post('/api/v1/sms/send', json=keyed)).json()
    resp = await client.post(
        '/api/v1/sms/send', json={**keyed, 'destination_addr': '15550000001'}
    )
    assert resp.status == 200
    assert (await resp.json())['message_id'] == first['message_id']


@pytest.mark.asyncio
async def test_different_keys_queue_separate_messages(http):
    client, queue, store = http
    for key in ('k-1', 'k-2'):
        resp = await client.post(
            '/api/v1/sms/send', json={**BODY, 'idempotency_key': key}
        )
        assert resp.status == 200
    assert queue.qsize() == 2
    assert len(store.messages) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('key', ['k' * 65, '', 5, 'a\x00b', 'a\ud800b'])
async def test_bad_idempotency_key_is_rejected(http, key):
    client, queue, store = http
    resp = await client.post('/api/v1/sms/send', json={**BODY, 'idempotency_key': key})
    assert resp.status == 400
    assert 'idempotency_key' in (await resp.json())['details']
    assert queue.empty()
    assert store.messages == {}


@pytest.mark.asyncio
async def test_64_character_key_is_taken(http):
    client, _, store = http
    resp = await client.post(
        '/api/v1/sms/send', json={**BODY, 'idempotency_key': 'k' * 64}
    )
    assert resp.status == 200
    assert store.messages[(await resp.json())['message_id']].idempotency_key == 'k' * 64


@pytest.mark.asyncio
async def test_concurrent_repeat_hitting_the_constraint_is_a_repeat(http, monkeypatch):
    client, queue, store = http
    keyed = {**BODY, 'idempotency_key': 'k-1'}
    first = await (await client.post('/api/v1/sms/send', json=keyed)).json()
    lookup = store.get_by_idempotency_key
    calls = []

    async def misses_first(key):  # the other submit commits after this lookup
        calls.append(key)
        return None if len(calls) == 1 else await lookup(key)

    monkeypatch.setattr(store, 'get_by_idempotency_key', misses_first)
    resp = await client.post('/api/v1/sms/send', json=keyed)
    assert resp.status == 200
    assert (await resp.json())['message_id'] == first['message_id']
    assert calls == ['k-1', 'k-1']
    assert queue.qsize() == 1
    assert len(store.messages) == 1


@pytest.mark.asyncio
async def test_failed_key_lookup_is_503(http, monkeypatch):
    client, queue, store = http

    async def down(key):
        raise SQLAlchemyError('database down')

    monkeypatch.setattr(store, 'get_by_idempotency_key', down)
    resp = await client.post('/api/v1/sms/send', json={**BODY, 'idempotency_key': 'k'})
    assert resp.status == 503
    assert queue.empty()


@pytest.mark.asyncio
async def test_metrics_lists_every_metric_and_counts_a_submit(http):
    client, _, _ = http
    before = sample('porth_messages_submitted_total', protocol='http')
    await submit(client)
    assert sample('porth_messages_submitted_total', protocol='http') == before + 1
    resp = await client.get('/metrics')
    assert resp.status == 200
    assert resp.headers['Content-Type'].startswith('text/plain; version=')
    text = await resp.text()
    for name in (
        'porth_messages_submitted_total',
        'porth_messages_final_total',
        'porth_send_retries_total',
        'porth_smpp_errors_total',
        'porth_receipts_total',
        'porth_mo_total',
        'porth_messages_waiting',
        'porth_messages_retrying',
        'porth_smsc_bound',
        'porth_submit_seconds',
    ):
        assert f'# TYPE {name} ' in text


@pytest.mark.asyncio
async def test_status_reports_version_uptime_and_smscs(http):
    client, _, _ = http
    body = await (await client.get('/status')).json()
    assert body['version'] and body['uptime_seconds'] >= 0
    assert body['smsc'] == smsc_state()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'bound_a, status, body',
    [
        (True, 200, {'ready': True}),
        (False, 503, {'ready': False, 'unbound': ['a']}),
    ],
)
async def test_ready_is_503_while_an_smsc_is_unbound(
    uow_factory, queues, bound_a, status, body
):
    app = create_http_app(
        queues, ROUTER, uow_factory, Settings(), lambda: smsc_state(bound_a)
    )
    async with TestClient(TestServer(app)) as client:
        resp = await client.get('/ready')
        assert (resp.status, await resp.json()) == (status, body)


@pytest.mark.asyncio
async def test_health_carries_the_real_time(http):
    client, _, _ = http
    body = await (await client.get('/health')).json()
    at = datetime.strptime(body['timestamp'], '%Y-%m-%dT%H:%M:%SZ')
    at = at.replace(tzinfo=timezone.utc)
    assert abs(datetime.now(timezone.utc) - at) < timedelta(seconds=5)

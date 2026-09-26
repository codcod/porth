"""Unit tests for the HTTP API send and status endpoints."""

from datetime import datetime

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from porth.config.settings import Settings
from porth.core.message import MessageStatus
from porth.core.queue import MessageQueue
from porth.core.store import MessageStore
from porth.protocols.http.api import create_http_app

BODY = {
    'source_addr': 'porth',
    'destination_addr': '+306900000000',
    'message_text': 'hi',
}


@pytest_asyncio.fixture
async def http():
    queue, store = MessageQueue(), MessageStore()
    app = create_http_app(queue, store, Settings())
    async with TestClient(TestServer(app)) as client:
        yield client, queue, store


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
async def test_poll_shows_status_changes_of_the_stored_message(http):
    client, _, store = http
    message_id = await submit(client)
    message = store.get(message_id)
    message.status = MessageStatus.SENT
    message.sent_at = datetime(2026, 9, 26, 12, 30, 5)
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
async def test_dlr_url_is_rejected(http):
    client, queue, store = http
    resp = await client.post(
        '/api/v1/sms/send', json={**BODY, 'dlr_url': 'http://example.com/dlr'}
    )
    assert resp.status == 400
    assert 'dlr_url is not supported' in (await resp.json())['details']
    assert queue.empty()
    assert store._messages == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('empty', [None, ''])
async def test_empty_dlr_url_is_accepted(http, empty):
    client, queue, _ = http
    resp = await client.post('/api/v1/sms/send', json={**BODY, 'dlr_url': empty})
    assert resp.status == 200
    assert queue.qsize() == 1

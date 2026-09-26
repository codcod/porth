"""Unit tests for the Kannel-compatible sendsms endpoint."""

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from porth.core.queue import MessageQueue
from porth.protocols.kannel.api import create_kannel_app


@pytest_asyncio.fixture
async def kannel():
    queue = MessageQueue()
    async with TestClient(TestServer(create_kannel_app(queue))) as client:
        yield client, queue


@pytest.mark.asyncio
async def test_sendsms_queues_the_message(kannel):
    client, queue = kannel
    resp = await client.get(
        '/cgi-bin/sendsms',
        params={
            'username': 'u',
            'password': 'p',
            'to': '+306900000000',
            'from': 'porth',
            'text': 'hi',
        },
    )
    assert resp.status == 200
    assert (await resp.text()).startswith('0: Accepted for delivery')
    assert queue.qsize() == 1
    message = await queue.get()
    assert message.protocol == 'kannel'
    assert message.destination_addr == '+306900000000'
    assert message.message_text == 'hi'
    assert 'username' not in message.protocol_data


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', ['to', 'from', 'text'])
async def test_sendsms_requires_to_from_and_text(kannel, missing):
    client, queue = kannel
    params = {'to': '+306900000000', 'from': 'porth', 'text': 'hi'}
    del params[missing]
    resp = await client.get('/cgi-bin/sendsms', params=params)
    assert resp.status == 400
    assert (await resp.text()).startswith('3:')
    assert queue.empty()


@pytest.mark.asyncio
async def test_sendsms_rejects_multiple_recipients(kannel):
    client, queue = kannel
    resp = await client.get(
        '/cgi-bin/sendsms',
        params={'to': '+306900000000 +306911111111', 'from': 'porth', 'text': 'hi'},
    )
    assert resp.status == 400
    assert 'single recipient' in await resp.text()
    assert queue.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['POST', 'HEAD'])
async def test_sendsms_other_methods_are_not_allowed(kannel, method):
    client, queue = kannel
    resp = await client.request(
        method, '/cgi-bin/sendsms', params={'to': '+306900000000', 'text': 'hi'}
    )
    assert resp.status == 405
    assert queue.empty()

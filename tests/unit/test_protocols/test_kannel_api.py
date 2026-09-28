"""Unit tests for the Kannel-compatible sendsms endpoint."""

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy.exc import SQLAlchemyError

from porth.core.message import MessageStatus
from porth.protocols.kannel.api import create_kannel_app
from tests.unit.test_protocols.test_http_api import RecordingQueue

PARAMS = {'to': '+306900000000', 'from': 'porth', 'text': 'hi'}


@pytest_asyncio.fixture
async def kannel(uow_factory):
    app = create_kannel_app(RecordingQueue(uow_factory), uow_factory)
    async with TestClient(TestServer(app)) as client:
        yield client, app['message_queue']


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
    text = await resp.text()
    assert text.startswith('0: Accepted for delivery')
    assert queue.qsize() == 1
    message = await queue.get()
    message_id = text.split('Message-ID: ')[1]
    assert message.message_id == message_id
    stored = client.app['uow_factory'].repo.messages[message_id]
    assert stored.status == MessageStatus.QUEUED
    assert queue.committed_at_put == [True]
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
async def test_sendsms_queues_one_message_per_recipient(kannel, uow_factory):
    client, queue = kannel
    resp = await client.get(
        '/cgi-bin/sendsms', params={**PARAMS, 'to': '306900000010  306900000011'}
    )
    assert resp.status == 200
    first, *id_lines = (await resp.text()).split('\n')
    assert first == '0: Accepted for delivery'
    messages = [await queue.get(), await queue.get()]
    assert [m.destination_addr for m in messages] == ['306900000010', '306900000011']
    assert id_lines == [f'Message-ID: {m.message_id}' for m in messages]
    assert all(m.message_id in uow_factory.repo.messages for m in messages)
    assert queue.committed_at_put == [True, True]


@pytest.mark.asyncio
async def test_sendsms_drops_invalid_recipients(kannel):
    client, queue = kannel
    resp = await client.get(
        '/cgi-bin/sendsms', params={**PARAMS, 'to': '306900000012 abc'}
    )
    assert resp.status == 200
    assert queue.qsize() == 1
    assert (await queue.get()).destination_addr == '306900000012'


@pytest.mark.asyncio
async def test_sendsms_sends_a_repeated_number_once(kannel):
    client, queue = kannel
    resp = await client.get(
        '/cgi-bin/sendsms',
        params={**PARAMS, 'to': '306900000010 306900000011 306900000010'},
    )
    assert resp.status == 200
    assert (await resp.text()).count('Message-ID:') == 2
    messages = [await queue.get(), await queue.get()]
    assert [m.destination_addr for m in messages] == ['306900000010', '306900000011']
    assert queue.empty()


@pytest.mark.asyncio
async def test_sendsms_rejects_when_no_valid_recipient(kannel):
    client, queue = kannel
    resp = await client.get('/cgi-bin/sendsms', params={**PARAMS, 'to': 'abc'})
    assert resp.status == 400
    assert 'no valid recipient' in await resp.text()
    assert queue.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'sender, expected', [(None, 'ACME'), ('', 'ACME'), ('porth', 'porth')]
)
async def test_sendsms_default_sender_fills_only_a_missing_from(
    uow_factory, sender, expected
):
    app = create_kannel_app(
        RecordingQueue(uow_factory), uow_factory, default_sender='ACME'
    )
    async with TestClient(TestServer(app)) as client:
        params = {'to': '306900000015', 'text': 'hi'}
        if sender is not None:
            params['from'] = sender
        resp = await client.get('/cgi-bin/sendsms', params=params)
        assert resp.status == 200
        assert (await app['message_queue'].get()).source_addr == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['POST', 'HEAD'])
async def test_sendsms_other_methods_are_not_allowed(kannel, method):
    client, queue = kannel
    resp = await client.request(
        method, '/cgi-bin/sendsms', params={'to': '+306900000000', 'text': 'hi'}
    )
    assert resp.status == 405
    assert queue.empty()


@pytest.mark.asyncio
async def test_failed_commit_is_503_and_queues_nothing(kannel, uow_factory):
    client, queue = kannel
    uow_factory.fail = SQLAlchemyError('database down')
    resp = await client.get('/cgi-bin/sendsms', params=PARAMS)
    assert resp.status == 503
    assert (await resp.text()).startswith('3: Failed to send SMS')
    assert queue.empty()


@pytest.mark.asyncio
async def test_failed_commit_queues_none_of_several_recipients(kannel, uow_factory):
    client, queue = kannel
    uow_factory.fail = SQLAlchemyError('database down')
    resp = await client.get(
        '/cgi-bin/sendsms', params={**PARAMS, 'to': '306900000010 306900000011'}
    )
    assert resp.status == 503
    assert queue.empty()
    assert uow_factory.repo.messages == {}

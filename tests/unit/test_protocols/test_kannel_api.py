"""Unit tests for the Kannel-compatible sendsms endpoint."""

import logging

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from sqlalchemy.exc import SQLAlchemyError

from porth.config.settings import KannelUser
from porth.core.message import MessageStatus
from porth.protocols.kannel.api import KannelAccessLogger, create_kannel_app
from tests.unit.test_protocols.test_http_api import ROUTER, RecordingQueue

PARAMS = {'to': '+306900000000', 'from': 'porth', 'text': 'hi'}


def app_for(uow_factory, default_sender=None, users=None):
    queues = {'a': RecordingQueue(uow_factory), 'b': RecordingQueue(uow_factory)}
    return create_kannel_app(queues, ROUTER, uow_factory, default_sender, users)


@pytest_asyncio.fixture
async def kannel(uow_factory):
    app = app_for(uow_factory)
    async with TestClient(TestServer(app)) as client:
        yield client, app['queues']['a']


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
    app = app_for(uow_factory, default_sender='ACME')
    async with TestClient(TestServer(app)) as client:
        params = {'to': '306900000015', 'text': 'hi'}
        if sender is not None:
            params['from'] = sender
        resp = await client.get('/cgi-bin/sendsms', params=params)
        assert resp.status == 200
        assert (await app['queues']['a'].get()).source_addr == expected


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


NOT_ROUTABLE = 'Not routable. Do not try again.'


@pytest.mark.asyncio
async def test_unroutable_recipient_is_dropped(kannel, uow_factory):
    client, queue = kannel
    resp = await client.get(
        '/cgi-bin/sendsms', params={**PARAMS, 'to': '306900000005 15550000005'}
    )
    assert resp.status == 200
    first, *id_lines = (await resp.text()).split('\n')
    assert first == '0: Accepted for delivery'
    message = await queue.get()
    assert (message.destination_addr, message.smsc) == ('306900000005', 'a')
    assert id_lines == [f'Message-ID: {message.message_id}']
    assert list(uow_factory.repo.messages) == [message.message_id]
    assert client.app['queues']['b'].empty()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'params',
    [
        {'to': '15550000006 15550000007'},
        {'to': '306900000004', 'smsc': 'zz'},  # an unknown smsc is an error
    ],
    ids=['none-routable', 'unknown-smsc'],
)
async def test_nothing_routable_is_kannels_403(kannel, uow_factory, params):
    client, queue = kannel
    resp = await client.get('/cgi-bin/sendsms', params={**PARAMS, **params})
    assert resp.status == 403
    assert await resp.text() == NOT_ROUTABLE
    assert queue.empty() and client.app['queues']['b'].empty()
    assert uow_factory.repo.messages == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('smsc, expected', [('b', 'b'), ('', 'a')])
async def test_smsc_overrides_the_prefix_and_empty_is_absent(
    kannel, uow_factory, smsc, expected
):
    client, _ = kannel
    resp = await client.get(
        '/cgi-bin/sendsms', params={**PARAMS, 'to': '306900000004', 'smsc': smsc}
    )
    assert resp.status == 200
    message = await client.app['queues'][expected].get()
    assert uow_factory.repo.messages[message.message_id].smsc == expected


AUTH_FAILED = 'Authorization failed for sendsms'
CREDS = {'username': 'u', 'password': 'p'}


@pytest_asyncio.fixture
async def authed(uow_factory, request):
    allow_ip = getattr(request, 'param', [])
    app = app_for(uow_factory, users={'u': KannelUser(password='p', allow_ip=allow_ip)})
    # main.py's access logger, so no log the test sees can hold a password
    server = TestServer(app)
    await server.start_server(access_log_class=KannelAccessLogger)
    async with TestClient(server) as client:
        yield client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'creds', [CREDS, {'user': 'u', 'pass': 'p'}], ids=['username', 'user-alias']
)
async def test_right_credentials_are_accepted(authed, creds):
    resp = await authed.get('/cgi-bin/sendsms', params={**PARAMS, **creds})
    assert resp.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'params',
    [
        {**PARAMS, 'username': 'u', 'password': 'wrong'},
        {**PARAMS, 'username': 'x', 'password': 'p'},
        {**PARAMS, 'username': 'u'},
        PARAMS,
        {'username': 'u', 'password': 'wrong'},  # no to: still 403, auth runs first
    ],
    ids=['wrong-password', 'unknown-user', 'no-password', 'no-credentials', 'no-to'],
)
async def test_bad_credentials_are_kannels_403(authed, uow_factory, caplog, params):
    resp = await authed.get('/cgi-bin/sendsms', params=params)
    assert resp.status == 403
    assert await resp.text() == AUTH_FAILED
    assert authed.app['queues']['a'].empty() and authed.app['queues']['b'].empty()
    assert uow_factory.repo.messages == {}
    assert 'wrong' not in caplog.text


@pytest.mark.asyncio
async def test_rejection_logs_the_username_not_the_password(authed, caplog):
    params = {**PARAMS, 'username': 'u', 'password': 's3cret'}
    await authed.get('/cgi-bin/sendsms', params=params)
    assert "'u'" in caplog.text and 's3cret' not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('authed', [['127.0.0.0/8']], indirect=True)
async def test_caller_inside_allow_ip_is_accepted(authed):
    resp = await authed.get('/cgi-bin/sendsms', params={**PARAMS, **CREDS})
    assert resp.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize('authed', [['10.0.0.0/8']], indirect=True)
@pytest.mark.parametrize('headers', [{}, {'X-Forwarded-For': '10.1.2.3'}])
async def test_caller_outside_allow_ip_is_rejected(authed, uow_factory, headers):
    resp = await authed.get(
        '/cgi-bin/sendsms', params={**PARAMS, **CREDS}, headers=headers
    )
    assert resp.status == 403
    assert await resp.text() == AUTH_FAILED
    assert uow_factory.repo.messages == {}


def test_no_users_warns_that_sendsms_is_open(uow_factory, caplog):
    app_for(uow_factory)
    assert 'sendsms accepts any caller' in caplog.text


def test_access_log_leaves_out_the_query_string(caplog):
    caplog.set_level(logging.INFO, logger='t')
    request = make_mocked_request('GET', '/cgi-bin/sendsms?username=u&password=s3cret')
    KannelAccessLogger(logging.getLogger('t'), '').log(request, web.Response(), 0.0)
    assert '/cgi-bin/sendsms' in caplog.text and 's3cret' not in caplog.text

"""Unit tests for the Kannel-compatible sendsms endpoint."""

import logging
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from sqlalchemy.exc import SQLAlchemyError

from porth.config.settings import KannelUser
from porth.domain.model import MessageStatus
from porth.entrypoints.kannel_api import KannelAccessLogger, create_kannel_app
from tests.conftest import sample
from tests.e2e.test_http_api import ROUTER, RecordingQueue

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
    assert resp.status == 202
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
@pytest.mark.parametrize(
    'missing, answer',
    [
        ('to', 'Missing receiver number, rejected'),
        ('from', 'Sender missing and no global set, rejected'),
    ],
)
async def test_sendsms_requires_to_and_from(kannel, missing, answer):
    client, queue = kannel
    params = {'to': '+306900000000', 'from': 'porth', 'text': 'hi'}
    del params[missing]
    resp = await client.get('/cgi-bin/sendsms', params=params)
    assert resp.status == 400
    assert await resp.text() == answer
    assert resp.content_type == 'text/html'
    assert queue.empty()


@pytest.mark.asyncio
async def test_sendsms_queues_one_message_per_recipient(kannel, uow_factory):
    client, queue = kannel
    submitted = sample('porth_messages_submitted_total', protocol='kannel')
    resp = await client.get(
        '/cgi-bin/sendsms', params={**PARAMS, 'to': '306900000010  306900000011'}
    )
    assert resp.status == 202
    assert sample('porth_messages_submitted_total', protocol='kannel') == submitted + 2
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
    assert resp.status == 202
    assert queue.qsize() == 1
    assert (await queue.get()).destination_addr == '306900000012'


@pytest.mark.asyncio
async def test_sendsms_sends_a_repeated_number_once(kannel):
    client, queue = kannel
    resp = await client.get(
        '/cgi-bin/sendsms',
        params={**PARAMS, 'to': '306900000010 306900000011 306900000010'},
    )
    assert resp.status == 202
    assert (await resp.text()).count('Message-ID:') == 2
    messages = [await queue.get(), await queue.get()]
    assert [m.destination_addr for m in messages] == ['306900000010', '306900000011']
    assert queue.empty()


@pytest.mark.asyncio
async def test_sendsms_rejects_when_no_valid_recipient(kannel):
    client, queue = kannel
    resp = await client.get('/cgi-bin/sendsms', params={**PARAMS, 'to': 'abc'})
    assert resp.status == 400
    assert (
        await resp.text()
        == 'Number(s) has/have been denied by white- and/or black-lists.'
    )
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
        assert resp.status == 202
        assert (await app['queues']['a'].get()).source_addr == expected


@pytest.mark.asyncio
async def test_sendsms_head_is_not_allowed(kannel):
    client, queue = kannel
    resp = await client.request(
        'HEAD', '/cgi-bin/sendsms', params={'to': '+306900000000', 'text': 'hi'}
    )
    assert resp.status == 405
    assert queue.empty()


@pytest.mark.asyncio
async def test_failed_commit_is_503_and_queues_nothing(kannel, uow_factory):
    client, queue = kannel
    uow_factory.fail = SQLAlchemyError('database down')
    resp = await client.get('/cgi-bin/sendsms', params=PARAMS)
    assert resp.status == 503
    assert await resp.text() == 'Temporal failure, try again later.'
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
    assert resp.status == 202
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
    assert resp.status == 202
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
    assert resp.status == 202


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
    assert resp.status == 202


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


ACCEPTED = '0: Accepted for delivery'
UDH = '%05%00%03%A1%02%01'


async def stored_from(client, resp):
    """The one message an accepted sendsms stored."""
    (line,) = [x for x in (await resp.text()).split('\n') if x.startswith('Message-ID')]
    return client.app['uow_factory'].repo.messages[line.split(': ')[1]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'query, status, answer, stored',
    [
        ('text=hi', 202, ACCEPTED, {'data_coding': 0x00, 'text': 'hi'}),
        ('text=hi&flash=1', 202, ACCEPTED, {'data_coding': 0xF0}),
        ('text=hi&flash=1&mclass=2', 202, ACCEPTED, {'data_coding': 0xF2}),
        ('text=hi&mclass=0', 202, ACCEPTED, {'data_coding': 0xF0}),
        ('text=hi&mclass=3', 202, ACCEPTED, {'data_coding': 0xF3}),
        ('text=hi&mclass=4', 400, 'MClass field misformed, rejected', None),
        ('text=hi&mclass=x', 202, ACCEPTED, {'data_coding': 0x00}),
        (
            'text=%03%93&coding=2&mclass=0',
            202,
            ACCEPTED,
            {'data_coding': 0x18, 'text': 'Γ'},
        ),
        (
            'text=hi&coding=1&mclass=1',
            202,
            ACCEPTED,
            {'data_coding': 0xF5, 'data': '6869'},
        ),
        (
            'text=%01%02%FF&coding=1',
            202,
            ACCEPTED,
            {'data_coding': 0x04, 'text': '', 'data': '0102ff'},
        ),
        (
            'text=%01%00%FF&coding=1',
            202,
            ACCEPTED,
            {'data_coding': 0x04, 'text': '', 'data': '0100ff'},
        ),
        # coding=2 without charset: the bytes are taken as UTF-16BE, as Kannel does
        (
            'text=%CE%93&coding=2',
            202,
            ACCEPTED,
            {'data_coding': 0x08, 'text': b'\xce\x93'.decode('utf-16-be')},
        ),
        (
            'text=%03%93&coding=2&charset=UTF-16BE',
            202,
            ACCEPTED,
            {'data_coding': 0x08, 'text': 'Γ'},
        ),
        # no coding: GSM if it can carry the text, else UCS-2 (not Kannel's '?')
        ('text=%CE%B1', 202, ACCEPTED, {'data_coding': 0x08, 'text': 'α'}),
        ('text=%CE%93', 202, ACCEPTED, {'data_coding': 0x00, 'text': 'Γ'}),
        ('text=%CE%B1&coding=0', 202, ACCEPTED, {'data_coding': 0x08}),
        (
            'text=caf%E9&charset=ISO-8859-1',
            202,
            ACCEPTED,
            {'data_coding': 0x00, 'text': 'café'},
        ),
        ('text=hi&charset=NOPE', 400, 'Charset or body misformed, rejected', None),
        # Python codecs that are no charset: undefined raises, punycode is quadratic
        ('text=hi&charset=undefined', 400, 'Charset or body misformed, rejected', None),
        (
            'text=a-bbbbbbbbbb&charset=punycode',
            400,
            'Charset or body misformed, rejected',
            None,
        ),
        ('text=hi&charset=utf-8%00', 400, 'Charset or body misformed, rejected', None),
        ('text=hi&charset=idna', 400, 'Charset or body misformed, rejected', None),
        ('text=%FF', 400, 'Charset or body misformed, rejected', None),
        ('text=hi&coding=3', 400, 'Coding field misformed, rejected', None),
        ('text=hi&coding=x', 202, ACCEPTED, {'data_coding': 0x00}),
        (
            f'text=p1&udh={UDH}',
            202,
            ACCEPTED,
            {'data_coding': 0x04, 'text': '', 'data': '7031'},
        ),
        (f'text=p1&udh={UDH}&coding=0', 202, ACCEPTED, {'data_coding': 0x00}),
        (f'text=p1&udh={UDH}&coding=2', 202, ACCEPTED, {'data_coding': 0x08}),
        (
            f'text=%CE%B1&udh={UDH}&coding=0',
            400,
            'Charset or body misformed, rejected',
            None,
        ),
        ('text=p1&udh=%05%00%03', 400, 'UDH field misformed, rejected', None),
        (f'udh={UDH}', 202, ACCEPTED, {'data_coding': 0x04, 'data': ''}),
        (f'text={"a" * 135}&udh={UDH}', 400, 'UDH field is too long, rejected', None),
        ('', 202, ACCEPTED, {'data_coding': 0x00, 'text': ''}),
        ('text=', 202, ACCEPTED, {'text': ''}),
        ('text=hi&dlr-mask=8', 400, 'DLR-Mask field misformed, rejected', None),
        ('text=hi&dlr-mask=-1', 400, 'DLR-Mask field misformed, rejected', None),
        ('text=hi&dlr-mask=19', 202, ACCEPTED, {'dlr_mask': 19}),
        ('text=hi&dlr-mask=x', 202, ACCEPTED, {'dlr_mask': 0}),
        ('text=hi', 202, ACCEPTED, {'dlr_mask': 0, 'dlr_url': None}),
        (
            'text=hi&dlrmask=3&dlrurl=http://x/%25d',
            202,
            ACCEPTED,
            {'dlr_mask': 3, 'dlr_url': 'http://x/%d'},
        ),
        (
            'text=hi&dlr-mask=1&dlrmask=2&dlr-url=http://a&dlrurl=http://b',
            202,
            ACCEPTED,
            {'dlr_mask': 1, 'dlr_url': 'http://a'},
        ),
        ('text=hi&validity=10', 202, ACCEPTED, {'validity': 10}),
        ('text=hi&validity=x', 202, ACCEPTED, {'validity': None}),
        ('text=hi&validity=0', 400, 'Validity field misformed, rejected', None),
        # submit_sm's two-digit year: 2100 or later, or past datetime's range
        ('text=hi&validity=60000000', 400, 'Validity field misformed, rejected', None),
        (
            'text=hi&validity=1000000000000',
            400,
            'Validity field misformed, rejected',
            None,
        ),
        ('text=hi&deferred=10', 400, 'Deferred field misformed, rejected', None),
        ('text=hi&priority=2', 400, 'Priority field misformed, rejected', None),
        ('text=hi&priority=9', 400, 'Priority field misformed, rejected', None),
    ],
)
async def test_sendsms_answers_as_kannel(kannel, query, status, answer, stored):
    client, queue = kannel
    resp = await client.get(f'/cgi-bin/sendsms?to=306900000001&from=porth&{query}')
    assert resp.status == status
    assert (await resp.text()).split('\n')[0] == answer
    assert resp.content_type == 'text/html'
    if stored is None:
        assert queue.empty()
        return
    message = await stored_from(client, resp)
    data = message.protocol_data
    if 'data_coding' in stored:
        assert data['data_coding'] == stored['data_coding']
    if 'udh=' in query:
        assert data['udh'] == '050003a10201'
    else:
        assert 'udh' not in data
    if 'text' in stored:
        assert message.message_text == stored['text']
    assert data.get('data') == stored.get('data')
    if 'dlr_mask' in stored:
        assert data['dlr_mask'] == stored['dlr_mask']
    if 'dlr_url' in stored:
        assert message.dlr_url == stored['dlr_url']
    if 'validity' in stored:
        if stored['validity'] is None:
            assert message.valid_until is None
        else:
            left = message.valid_until - datetime.now(timezone.utc)
            assert timedelta(minutes=9) < left <= timedelta(minutes=10)


def kannel_headers(**extra):
    return {
        'X-Kannel-Username': 'u',
        'X-Kannel-Password': 'p',
        'X-Kannel-From': 'porth',
        'X-Kannel-To': '306900000001',
        **extra,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'headers, body, stored',
    [
        (
            {'Content-Type': 'text/plain'},
            b'hi',
            {'data_coding': 0x00, 'text': 'hi'},
        ),
        (
            {'Content-Type': 'text/plain; charset=UTF-8', 'X-Kannel-Coding': '2'},
            'Γ'.encode(),
            {'data_coding': 0x08, 'text': 'Γ'},
        ),
        (
            {'Content-Type': 'text/plain; charset=ISO-8859-1'},
            b'caf\xe9',
            {'data_coding': 0x00, 'text': 'café'},
        ),
        (
            {'Content-Type': 'application/octet-stream'},
            b'\x01\x00\xff',
            {'data_coding': 0x04, 'text': '', 'data': '0100ff'},
        ),
        (
            {'Content-Type': 'application/octet-stream', 'X-Kannel-Coding': '0'},
            b'hi',
            {'data_coding': 0x00, 'text': 'hi'},
        ),
        (
            # hex, as smsbox reads X-Kannel-UDH first
            {'Content-Type': 'text/plain', 'X-Kannel-UDH': '050003A10201'},
            b'p1',
            {'data_coding': 0x04, 'udh': '050003a10201', 'data': '7031'},
        ),
        (
            {
                'Content-Type': 'text/plain',
                'X-Kannel-MClass': '1',
                'X-Kannel-DLR-Mask': '3',
                'X-Kannel-DLR-Url': 'http://x/%d',
                'X-Kannel-UDH': UDH,
            },
            b'p1',
            {
                'data_coding': 0xF5,
                'udh': '050003a10201',
                'data': '7031',
                'dlr_mask': 3,
                'dlr_url': 'http://x/%d',
            },
        ),
    ],
)
async def test_sendsms_post(authed, headers, body, stored):
    resp = await authed.post(
        '/cgi-bin/sendsms', headers=kannel_headers(**headers), data=body
    )
    assert resp.status == 202
    assert resp.content_type == 'text/html'
    message = await stored_from(authed, resp)
    data = message.protocol_data
    assert data['data_coding'] == stored['data_coding']
    assert data.get('udh') == stored.get('udh')
    assert data.get('data') == stored.get('data')
    if 'text' in stored:
        assert message.message_text == stored['text']
    if 'dlr_mask' in stored:
        assert (data['dlr_mask'], message.dlr_url) == (
            stored['dlr_mask'],
            stored['dlr_url'],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'content_type', ['image/png', 'application/x-www-form-urlencoded']
)
async def test_sendsms_post_other_content_type_is_refused_before_credentials(
    authed, content_type
):
    resp = await authed.post(
        '/cgi-bin/sendsms', headers={'Content-Type': content_type}, data=b'x'
    )
    assert resp.status == 400
    assert await resp.text() == 'Invalid content-type'


@pytest.mark.asyncio
async def test_sendsms_post_reads_no_query_string(authed):
    resp = await authed.post(
        '/cgi-bin/sendsms',
        params={**CREDS, **PARAMS},
        headers={'Content-Type': 'text/plain'},
        data=b'hi',
    )
    assert resp.status == 403
    assert await resp.text() == AUTH_FAILED


@pytest.mark.asyncio
async def test_sendsms_post_refuses_a_codec_that_is_no_charset(authed):
    resp = await authed.post(
        '/cgi-bin/sendsms',
        headers=kannel_headers(**{'Content-Type': 'text/plain; charset=punycode'}),
        data=b'a-' + b'b' * 100,
    )
    assert resp.status == 400
    assert await resp.text() == 'Charset or body misformed, rejected'


@pytest.mark.asyncio
async def test_sendsms_text_longer_than_255_parts_can_carry_is_refused(authed):
    resp = await authed.post(
        '/cgi-bin/sendsms',
        headers=kannel_headers(**{'Content-Type': 'text/plain'}),
        data=b'a' * (255 * 160 * 4 + 1),
    )
    assert resp.status == 400
    assert await resp.text() == 'Charset or body misformed, rejected'

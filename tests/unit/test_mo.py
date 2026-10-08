"""Unit tests for the MO handler (Kannel get-url escape codes and reply rules)."""

import asyncio
import socket
import types
from datetime import datetime, timezone
from urllib.parse import unquote_to_bytes

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from smpp import Address, DataCoding, Message, TonType

from porth.config.settings import MOConfig
from porth.service_layer.dlr import expand_url
from porth.service_layer.mo import MOHandler, mo_values
from porth.domain.model import MessageStatus
from porth.adapters.queue import MessageQueue
from tests.conftest import FakeUowFactory, sample

NOW = datetime(2026, 9, 27, 10, 28, 44, tzinfo=timezone.utc)
TEMPLATE = 'p=%p&P=%P&k=%k&r=%r&a=%a&b=%b&t=%t&T=%T&c=%c&C=%C'
UCS2_WORDS = (
    '%03%93%03%B5%03%B9%03%AC%00+%03%C3%03%BF%03%C5%00+%03%BA%03%CC%03%C3%03%BC%03%B5'
)

# The requests Kannel 1.4.5 made for these MOs (ticket POR-015), t/T at NOW
KANNEL = [
    (
        '306900000001',
        TonType.INTERNATIONAL,
        DataCoding.DEFAULT,
        'Hello  world  again',
        'p=%2B306900000001&P=1234&k=Hello&r=world+again&a=Hello+world+again'
        '&b=Hello++world++again&t=2026-09-27+10:28:44&T=1790504924&c=0&C=UTF-8',
    ),
    (
        '6900000002',
        TonType.UNKNOWN,
        DataCoding.DEFAULT,
        'Price 5€ & more?',
        'p=6900000002&P=1234&k=Price&r=5%E2%82%AC+%26+more%3F'
        '&a=Price+5%E2%82%AC+%26+more%3F&b=Price+5%E2%82%AC+%26+more%3F'
        '&t=2026-09-27+10:28:44&T=1790504924&c=0&C=UTF-8',
    ),
    (
        '306900000003',
        TonType.INTERNATIONAL,
        DataCoding.UCS2,
        'Γειά σου κόσμε',
        'p=%2B306900000003&P=1234&k=%03%93%03%B5%03%B9%03%AC%00'
        '&r=%03%C3%03%BF%03%C5%00+%03%BA%03%CC%03%C3%03%BC%03%B5'
        f'&a={UCS2_WORDS}&b={UCS2_WORDS}'
        '&t=2026-09-27+10:28:44&T=1790504924&c=2&C=UTF-16BE',
    ),
]


def mo(sender, ton=TonType.INTERNATIONAL, text='Hello world', coding=0) -> Message:
    return Message(
        sender=Address(sender, ton),
        to=Address('1234'),
        text=text,
        receipt=None,
        pdu=types.SimpleNamespace(data_coding=coding),
    )


def decoded(query: str) -> dict[str, bytes]:
    """Query values as bytes, '+' read as a space (as any query parser does)."""
    pairs = (p.split('=', 1) for p in query.split('&'))
    return {k: unquote_to_bytes(v.replace('+', ' ')) for k, v in pairs}


@pytest.mark.parametrize('sender,ton,coding,text,kannel', KANNEL)
def test_mo_values_match_kannel(sender, ton, coding, text, kannel):
    url = expand_url(TEMPLATE, mo_values(mo(sender, ton, text, coding), NOW))
    assert decoded(url) == decoded(kannel)


def test_international_sender_already_carrying_plus_gets_one():
    values = mo_values(mo('+306900000009'), NOW)
    assert values['p'] == '+306900000009'


def test_expand_url_percent_rules():
    assert expand_url('a=%%k&b=%k&c=%s', {'k': b'x y'}) == 'a=%k&b=x%20y&c=%s'


class App:
    """A local MO application answering one fixed response."""

    def __init__(self, response: web.Response):
        self.requests: list[web.Request] = []
        self.response = response

    async def __call__(self, request: web.Request) -> web.Response:
        self.requests.append(request)
        return self.response


async def forward(
    response=None, reply=True, url=None, fail=None, sender='306900000001', query='k=%k'
):
    """Hand one MO, arriving on SMSC a, to a started MOHandler; return (app, queue,
    store), queue being a's."""
    app = App(response or web.Response(text='Thanks'))
    web_app = web.Application()
    web_app.router.add_get('/mo', app)
    server = TestServer(web_app)
    await server.start_server()
    queues, uow_factory = {'a': MessageQueue(), 'b': MessageQueue()}, FakeUowFactory()
    uow_factory.fail = fail
    if url is None:
        url = str(server.make_url('/mo')) + '?' + query
    handler = MOHandler(queues, uow_factory, MOConfig(url=url or None, reply=reply))
    await handler.start()
    try:
        handler.on_mo(mo(sender), 'a')
        await asyncio.gather(*handler._tasks)
    finally:
        await handler.stop()
        await server.close()
    assert queues['b'].empty()  # a reply never leaves through another SMSC
    return app, queues['a'], uow_factory.repo


@pytest.mark.asyncio
async def test_text_plain_reply_is_queued_and_stored():
    app, queue, store = await forward()
    assert [r.query['k'] for r in app.requests] == ['Hello']
    assert queue.qsize() == 1
    reply = await queue.get()
    assert (reply.source_addr, reply.destination_addr) == ('1234', '+306900000001')
    assert reply.message_text == 'Thanks'
    assert reply.protocol == 'kannel' and not reply.dlr_requested
    stored = store.messages[reply.message_id]
    assert stored.status == MessageStatus.QUEUED
    assert stored.message_text == 'Thanks'


@pytest.mark.asyncio
async def test_202_reply_is_sent_with_blanks_stripped():
    _, queue, _ = await forward(response=web.Response(text=' Thanks\n', status=202))
    assert (await queue.get()).message_text == 'Thanks'


def closed_port_url() -> str:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return f'http://127.0.0.1:{s.getsockname()[1]}/mo'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'kwargs',
    [
        dict(response=web.Response(text='')),
        dict(response=web.Response(text=' \n')),
        dict(response=web.Response(text='<p>Thanks</p>', content_type='text/html')),
        dict(response=web.Response(text='Thanks', status=500)),
        dict(url=closed_port_url()),
        dict(reply=False),
    ],
    ids=['empty', 'blank', 'html', '500', 'connection-error', 'reply-off'],
)
async def test_no_reply_is_sent(kwargs):
    _, queue, _ = await forward(**kwargs)
    assert queue.empty()


@pytest.mark.asyncio
async def test_unset_url_makes_no_request():
    app, queue, _ = await forward(url='')
    assert app.requests == [] and queue.empty()


@pytest.mark.asyncio
async def test_reply_not_stored_is_not_sent(caplog):
    _, queue, store = await forward(fail=OSError('database down'))
    assert queue.empty()
    assert 'reply not stored, not sent' in caplog.text


@pytest.mark.asyncio
async def test_reply_goes_back_through_the_arriving_smsc():
    # Were it routed, +44... could go elsewhere; the MO arrived on a, so a it is
    _, queue, store = await forward(sender='447000000001')
    reply = await queue.get()
    assert reply.smsc == 'a'
    assert store.messages[reply.message_id].smsc == 'a'


@pytest.mark.asyncio
async def test_percent_i_is_the_arriving_smsc():
    app, _, _ = await forward(query='i=%i')
    assert [dict(r.query) for r in app.requests] == [{'i': 'a'}]


@pytest.mark.asyncio
async def test_forwarded_and_failed_mo_are_counted():
    forwarded = sample('porth_mo_total', smsc='a', result='forwarded')
    failed = sample('porth_mo_total', smsc='a', result='failed')
    await forward()
    await forward(url=closed_port_url())
    assert sample('porth_mo_total', smsc='a', result='forwarded') == forwarded + 1
    assert sample('porth_mo_total', smsc='a', result='failed') == failed + 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'status, result', [(200, 'failed'), (500, 'forwarded')], ids=['reply', 'no-reply']
)
async def test_undecodable_body_fails_only_a_reply(status, result):
    # Read, strictly, only when it is a reply: a bad one fails the MO, as before
    count = sample('porth_mo_total', smsc='a', result=result)
    bad = web.Response(
        body=b'caf\xe9', status=status, content_type='text/plain', charset='utf-8'
    )
    _, queue, _ = await forward(response=bad)
    assert queue.empty()
    assert sample('porth_mo_total', smsc='a', result=result) == count + 1

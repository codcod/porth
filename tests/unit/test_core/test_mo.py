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
from porth.core.dlr import expand_url
from porth.core.mo import MOHandler, mo_values
from porth.core.queue import MessageQueue
from porth.core.store import MessageStore

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


async def forward(response=None, reply=True, url=None):
    """Hand one MO to a started MOHandler; return (app, queue, store)."""
    app = App(response or web.Response(text='Thanks'))
    web_app = web.Application()
    web_app.router.add_get('/mo', app)
    server = TestServer(web_app)
    await server.start_server()
    queue, store = MessageQueue(), MessageStore()
    if url is None:
        url = str(server.make_url('/mo')) + '?k=%k'
    handler = MOHandler(queue, store, MOConfig(url=url or None, reply=reply))
    await handler.start()
    try:
        handler.on_mo(mo('306900000001'))
        await asyncio.gather(*handler._tasks)
    finally:
        await handler.stop()
        await server.close()
    return app, queue, store


@pytest.mark.asyncio
async def test_text_plain_reply_is_queued_and_stored():
    app, queue, store = await forward()
    assert [r.query['k'] for r in app.requests] == ['Hello']
    assert queue.qsize() == 1
    reply = await queue.get()
    assert (reply.source_addr, reply.destination_addr) == ('1234', '+306900000001')
    assert reply.message_text == 'Thanks'
    assert reply.protocol == 'kannel' and not reply.dlr_requested
    assert store.get(reply.message_id) is reply


def closed_port_url() -> str:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return f'http://127.0.0.1:{s.getsockname()[1]}/mo'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'kwargs',
    [
        dict(response=web.Response(text='')),
        dict(response=web.Response(text='<p>Thanks</p>', content_type='text/html')),
        dict(response=web.Response(text='Thanks', status=500)),
        dict(url=closed_port_url()),
        dict(reply=False),
    ],
    ids=['empty', 'html', '500', 'connection-error', 'reply-off'],
)
async def test_no_reply_is_sent(kwargs):
    _, queue, _ = await forward(**kwargs)
    assert queue.empty()


@pytest.mark.asyncio
async def test_unset_url_makes_no_request():
    app, queue, _ = await forward(url='')
    assert app.requests == [] and queue.empty()

"""Integration test: real submit_sm over TCP to an smppai test SMSC."""

import asyncio
import functools
import pathlib
import socket
import ssl

import aiohttp

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from smpp import NpiType, SMPPServer, TonType

from porth.config.settings import (
    DeliveryConfig,
    HTTPConfig,
    KannelConfig,
    MOConfig,
    RoutingConfig,
    Settings,
    SMPPClientConfig,
)
from porth.service_layer.delivery import DeliveryEngine
from porth.service_layer.dlr import DLRHandler
from porth.domain.model import MessageStatus, SMSMessage
from porth.service_layer.mo import MOHandler
from porth.adapters.queue import MessageQueue
from porth.entrypoints.app import Gateway
from porth.adapters.smpp import SMPPClient
from tests.conftest import FakeUowFactory


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.mark.asyncio
async def test_smpp_message_flow(uow_factory):
    """A message routed through the engine reaches the SMSC as submit_sm."""
    port = free_port()
    received = []
    server = SMPPServer(host='127.0.0.1', port=port, setup_signal_handlers=False)

    def on_message_received(server, session, pdu):
        received.append(pdu)
        return 'smsc-1'

    server.on_message_received = on_message_received

    porth_client = SMPPClient(
        'a',
        SMPPClientConfig(host='127.0.0.1', port=port, system_id='porth', password='pw'),
    )
    engine = DeliveryEngine(
        'a',
        porth_client,
        MessageQueue(),
        uow_factory,
        Settings(),
        DLRHandler(uow_factory),
    )
    message = SMSMessage(
        source_addr='1234',
        destination_addr='5678',
        message_text='Hello @ €',
        protocol='http',
        smsc='a',
    )

    await uow_factory.repo.add(message)
    await server.start()
    try:
        await engine._process_message(message)
    finally:
        await porth_client.disconnect()
        await server.stop()

    assert len(received) == 1
    assert received[0].data_coding == 0
    assert received[0].get_message_text() == 'Hello @ €'
    assert uow_factory.repo.messages[message.message_id].status == MessageStatus.SENT
    assert uow_factory.repo.smsc_ids == {('a', 'smsc-1'): message.message_id}


@pytest.mark.asyncio
async def test_long_message_goes_out_in_parts(uow_factory):
    """Text longer than one SMS reaches the SMSC as UDH-concatenated submit_sm parts."""
    port = free_port()
    received = []
    server = SMPPServer(host='127.0.0.1', port=port, setup_signal_handlers=False)

    def on_message_received(server, session, pdu):
        received.append(pdu)
        return f'smsc-{len(received)}'

    server.on_message_received = on_message_received

    porth_client = SMPPClient(
        'a',
        SMPPClientConfig(host='127.0.0.1', port=port, system_id='porth', password='pw'),
    )
    engine = DeliveryEngine(
        'a',
        porth_client,
        MessageQueue(),
        uow_factory,
        Settings(),
        DLRHandler(uow_factory),
    )
    message = SMSMessage(
        source_addr='1234',
        destination_addr='5678',
        message_text='a' * 200,
        protocol='http',
        smsc='a',
    )

    await uow_factory.repo.add(message)
    await server.start()
    try:
        await engine._process_message(message)
    finally:
        await porth_client.disconnect()
        await server.stop()

    assert len(received) == 2
    assert all(pdu.esm_class & 0x40 for pdu in received)  # UDHI set by smppai
    stored = uow_factory.repo.messages[message.message_id]
    assert stored.status == MessageStatus.SENT
    assert stored.protocol_data['smsc_message_ids'] == ['smsc-1', 'smsc-2']


RECEIPT = (
    'id:smsc-1 sub:001 dlvrd:001 submit date:2609261200 done date:2609261201 '
    'stat:DELIVRD err:000 text:'
)


async def send_and_receipt(message: SMSMessage) -> SMSMessage:
    """Send message to an smppai SMSC that answers 'smsc-1', then deliver its receipt;
    return the stored message."""
    uow_factory = FakeUowFactory()
    store = uow_factory.repo
    port = free_port()
    server = SMPPServer(host='127.0.0.1', port=port, setup_signal_handlers=False)
    server.on_message_received = lambda server, session, pdu: 'smsc-1'

    handler = DLRHandler(uow_factory)
    porth_client = SMPPClient(
        'a',
        SMPPClientConfig(host='127.0.0.1', port=port, system_id='porth', password='pw'),
        on_receipt=functools.partial(handler.on_receipt, smsc='a'),
    )
    engine = DeliveryEngine(
        'a',
        porth_client,
        MessageQueue(),
        uow_factory,
        Settings(),
        DLRHandler(uow_factory),
    )
    message.smsc = 'a'
    await store.add(message)

    await server.start()
    await handler.start()
    try:
        await engine._process_message(message)
        assert await server.deliver_sm(
            'porth',
            source_addr='5678',
            destination_addr='1234',
            short_message=RECEIPT,
            esm_class=0x04,
        )
        async with asyncio.timeout(5):
            while store.messages[message.message_id].status == MessageStatus.SENT:
                await asyncio.sleep(0.01)
            await asyncio.gather(*handler._tasks)
    finally:
        await porth_client.disconnect()
        await handler.stop()
        await server.stop()
    return store.messages[message.message_id]


@pytest.mark.asyncio
async def test_receipt_marks_message_delivered():
    message = SMSMessage(
        source_addr='1234', destination_addr='5678', message_text='hi', protocol='http'
    )
    stored = await send_and_receipt(message)
    assert stored.status == MessageStatus.DELIVERED
    assert stored.delivered_at is not None


@pytest.mark.asyncio
async def test_tx_sends_rx_takes_the_receipt_and_tx_sends_without_rx():
    """transceiver = false: submits go over the transmitter bind, the receipt comes
    over the receiver bind, and a send still goes out with the receiver unbound."""
    uow_factory = FakeUowFactory()
    store = uow_factory.repo
    port = free_port()
    server = SMPPServer(host='127.0.0.1', port=port, setup_signal_handlers=False)
    server.on_message_received = lambda server, session, pdu: 'smsc-1'

    handler = DLRHandler(uow_factory)
    config = SMPPClientConfig(
        host='127.0.0.1', port=port, system_id='porth', password='pw', transceiver=False
    )
    on_receipt = functools.partial(handler.on_receipt, smsc='a')
    tx = SMPPClient('a', config, on_receipt=on_receipt, bind='tx')
    rx = SMPPClient('a', config, on_receipt=on_receipt, bind='rx')
    engine = DeliveryEngine(
        'a', tx, MessageQueue(), uow_factory, Settings(), DLRHandler(uow_factory)
    )
    first, second = (
        SMSMessage(
            source_addr='1234',
            destination_addr='5678',
            message_text=text,
            protocol='http',
            smsc='a',
        )
        for text in ('one', 'two')
    )
    await store.add(first)
    await store.add(second)

    await server.start()
    await handler.start()
    try:
        await rx.connect()
        await engine._process_message(first)
        binds = sorted(s.bind_type for s in server.get_client_sessions())
        assert binds == ['receiver', 'transmitter']
        assert await server.deliver_sm(
            'porth',
            source_addr='5678',
            destination_addr='1234',
            short_message=RECEIPT,
            esm_class=0x04,
        )
        async with asyncio.timeout(5):
            while store.messages[first.message_id].status == MessageStatus.SENT:
                await asyncio.sleep(0.01)
            await asyncio.gather(*handler._tasks)
        assert store.messages[first.message_id].status == MessageStatus.DELIVERED

        await rx.disconnect()
        await engine._process_message(second)
        assert store.messages[second.message_id].status == MessageStatus.SENT
    finally:
        await tx.disconnect()
        await rx.disconnect()
        await handler.stop()
        await server.stop()


@pytest.mark.asyncio
async def test_receipt_calls_kannel_dlr_url():
    requests = []

    async def dlr(request):
        requests.append(request)
        return web.Response()

    app = web.Application()
    app.router.add_get('/dlr', dlr)
    dlr_server = TestServer(app)
    await dlr_server.start_server()
    message = SMSMessage(
        source_addr='1234',
        destination_addr='5678',
        message_text='hi',
        protocol='kannel',
        protocol_data={'dlr_mask': 1},
        dlr_url=str(dlr_server.make_url('/dlr')) + '?d=%d&f=%F',
    )
    try:
        stored = await send_and_receipt(message)
    finally:
        await dlr_server.close()
    assert stored.status == MessageStatus.DELIVERED
    assert [dict(r.query) for r in requests] == [{'d': '1', 'f': 'smsc-1'}]


@pytest.mark.asyncio
async def test_mo_reaches_the_application_and_its_reply_goes_back(uow_factory):
    """An MO deliver_sm is forwarded to mo.url; its text/plain reply is sent as submit_sm."""
    queries = []

    async def application(request):
        queries.append(dict(request.query))
        return web.Response(text='Thanks')

    app = web.Application()
    app.router.add_get('/mo', application)
    app_server = TestServer(app)
    await app_server.start_server()

    port = free_port()
    received = []
    server = SMPPServer(host='127.0.0.1', port=port, setup_signal_handlers=False)

    def on_message_received(server, session, pdu):
        received.append(pdu)
        return 'smsc-1'

    server.on_message_received = on_message_received

    queue = MessageQueue()
    handler = MOHandler(
        {'a': queue},
        uow_factory,
        MOConfig(url=str(app_server.make_url('/mo')) + '?k=%k&p=%p'),
    )
    porth_client = SMPPClient(
        'a',
        SMPPClientConfig(host='127.0.0.1', port=port, system_id='porth', password='pw'),
        on_mo=functools.partial(handler.on_mo, smsc='a'),
    )
    engine = DeliveryEngine(
        'a', porth_client, queue, uow_factory, Settings(), DLRHandler(uow_factory)
    )

    await server.start()
    await handler.start()
    await engine.start()
    try:
        await porth_client.connect()
        assert await server.deliver_sm(
            'porth',
            source_addr='306900000001',
            source_addr_ton=TonType.INTERNATIONAL,
            source_addr_npi=NpiType.ISDN,
            destination_addr='1234',
            short_message='Hello world',
        )
        async with asyncio.timeout(5):
            while not received:
                await asyncio.sleep(0.01)
    finally:
        await engine.stop()
        await porth_client.disconnect()
        await handler.stop()
        await server.stop()
        await app_server.close()

    assert queries == [{'k': 'Hello', 'p': '+306900000001'}]
    (pdu,) = received
    assert pdu.destination_addr == '306900000001'
    assert pdu.dest_addr_ton == TonType.INTERNATIONAL
    assert pdu.get_message_text() == 'Thanks'


@pytest.mark.asyncio
async def test_each_number_goes_out_through_its_prefixs_smsc(uow_factory):
    """Two smppai SMSCs, a (30...) and b (44...): each gets exactly its own number."""
    received: dict[str, list[str]] = {'a': [], 'b': []}
    servers, config = [], {}
    for name in received:
        port = free_port()
        server = SMPPServer(host='127.0.0.1', port=port, setup_signal_handlers=False)
        server.on_message_received = functools.partial(
            lambda got, server, session, pdu: got.append(pdu.destination_addr) or 'x',
            received[name],
        )
        servers.append(server)
        config[name] = SMPPClientConfig(
            host='127.0.0.1', port=port, system_id='porth', password='pw'
        )
    kannel_port = free_port()
    gateway = Gateway(
        Settings(
            http=HTTPConfig(host='127.0.0.1', port=0),
            kannel=KannelConfig(host='127.0.0.1', port=kannel_port),
            smsc=config,
            routing=RoutingConfig(prefixes={'30': 'a', '44': 'b'}),
        ),
        uow_factory,
    )
    for server in servers:
        await server.start()
    await gateway.start()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f'http://127.0.0.1:{kannel_port}/cgi-bin/sendsms',
                params={
                    'from': '1234',
                    'to': '306900000001 447000000001',
                    'text': 'hi',
                },
            ) as response:
                assert response.status == 202
        async with asyncio.timeout(5):
            while not (received['a'] and received['b']):
                await asyncio.sleep(0.01)
    finally:
        await gateway.stop()
        for server in servers:
            await server.stop()
    assert received == {'a': ['306900000001'], 'b': ['447000000001']}


@pytest.mark.asyncio
async def test_message_for_a_down_smsc_goes_out_through_its_fallback(uow_factory):
    """a never binds (closed port); after fallback_after its message moves to b,
    which sends it, and b's receipt marks it delivered."""
    received: list[str] = []
    server = SMPPServer(host='127.0.0.1', port=free_port(), setup_signal_handlers=False)
    server.on_message_received = lambda server, session, pdu: (
        received.append(pdu.destination_addr) or 'smsc-1'
    )
    config = {
        name: SMPPClientConfig(
            host='127.0.0.1', port=port, system_id='porth', password='pw'
        )
        for name, port in (('a', free_port()), ('b', server.port))
    }
    config['a'].fallback, config['a'].fallback_after = 'b', 1
    kannel_port = free_port()
    gateway = Gateway(
        Settings(
            http=HTTPConfig(host='127.0.0.1', port=0),
            kannel=KannelConfig(host='127.0.0.1', port=kannel_port),
            smsc=config,
            routing=RoutingConfig(prefixes={'30': 'a'}),
            delivery=DeliveryConfig(max_retries=5, retry_delay=1, backoff_factor=1),
        ),
        uow_factory,
    )
    store = uow_factory.repo
    await server.start()
    await gateway.start()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f'http://127.0.0.1:{kannel_port}/cgi-bin/sendsms',
                params={'from': '1234', 'to': '306900000001', 'text': 'hi'},
            ) as response:
                assert response.status == 202
        (message,) = store.messages.values()
        async with asyncio.timeout(10):
            while message.status != MessageStatus.SENT:
                await asyncio.sleep(0.01)
                message = store.messages[message.message_id]
        assert (message.smsc, received) == ('b', ['306900000001'])
        assert await server.deliver_sm(
            'porth',
            source_addr='306900000001',
            destination_addr='1234',
            short_message=RECEIPT,
            esm_class=0x04,
        )
        async with asyncio.timeout(5):
            while store.messages[message.message_id].status == MessageStatus.SENT:
                await asyncio.sleep(0.01)
    finally:
        await gateway.stop()
        await server.stop()
    assert store.messages[message.message_id].status == MessageStatus.DELIVERED


TLS_DIR = pathlib.Path(__file__).parents[1] / 'fixtures' / 'tls'


@pytest_asyncio.fixture
async def tls_server():
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(TLS_DIR / 'cert.pem', TLS_DIR / 'key.pem')
    server = SMPPServer(
        host='127.0.0.1', port=free_port(), setup_signal_handlers=False, ssl=ctx
    )
    server.received = []

    def on_message_received(server, session, pdu):
        server.received.append(pdu)
        return 'smsc-1'

    server.on_message_received = on_message_received
    await server.start()
    yield server
    await server.stop()


def tls_client(server, **kwargs) -> SMPPClient:
    config = SMPPClientConfig(
        host='localhost',
        port=server.port,
        system_id='porth',
        password='pw',
        tls=True,
        **kwargs,
    )
    return SMPPClient('a', config)


@pytest.mark.asyncio
async def test_tls_bind_sends_submit_sm(tls_server):
    """With the SMSC's CA, porth binds over TLS and a message arrives as submit_sm."""
    porth_client = tls_client(tls_server, tls_ca_file=str(TLS_DIR / 'cert.pem'))
    message = SMSMessage(
        source_addr='1234',
        destination_addr='5678',
        message_text='over tls',
        protocol='http',
        smsc='a',
    )
    try:
        await porth_client.send_message(message)
    finally:
        await porth_client.disconnect()
    assert [pdu.get_message_text() for pdu in tls_server.received] == ['over tls']


@pytest.mark.asyncio
async def test_tls_without_ca_file_refuses_untrusted_smsc(tls_server):
    """Verification is on: the system CAs do not trust the self-signed certificate."""
    porth_client = tls_client(tls_server)
    with pytest.raises(Exception, match='CERTIFICATE_VERIFY_FAILED'):
        await porth_client.connect()
    assert not tls_server.received

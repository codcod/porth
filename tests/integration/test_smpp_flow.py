"""Integration test: real submit_sm over TCP to an smppai test SMSC."""

import asyncio
import functools
import socket

import aiohttp

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from smpp import NpiType, SMPPServer, TonType

from porth.config.settings import (
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
                assert response.status == 200
        async with asyncio.timeout(5):
            while not (received['a'] and received['b']):
                await asyncio.sleep(0.01)
    finally:
        await gateway.stop()
        for server in servers:
            await server.stop()
    assert received == {'a': ['306900000001'], 'b': ['447000000001']}

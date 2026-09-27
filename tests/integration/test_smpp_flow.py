"""Integration test: real submit_sm over TCP to an smppai test SMSC."""

import asyncio
import socket

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from smpp import NpiType, SMPPServer, TonType

from porth.config.settings import MOConfig, Settings, SMPPClientConfig
from porth.core.delivery import DeliveryEngine
from porth.core.dlr import DLRHandler
from porth.core.message import MessageStatus, SMSMessage
from porth.core.mo import MOHandler
from porth.core.queue import MessageQueue
from porth.core.store import MessageStore
from porth.protocols.smpp.client import SMPPClient


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.mark.asyncio
async def test_smpp_message_flow():
    """A message routed through the engine reaches the SMSC as submit_sm."""
    port = free_port()
    received = []
    server = SMPPServer(host='127.0.0.1', port=port, setup_signal_handlers=False)

    def on_message_received(server, session, pdu):
        received.append(pdu)
        return 'smsc-1'

    server.on_message_received = on_message_received

    engine = DeliveryEngine(MessageQueue(), MessageStore(), Settings())
    porth_client = SMPPClient(
        SMPPClientConfig(host='127.0.0.1', port=port, system_id='porth', password='pw')
    )
    engine.smpp_client = porth_client
    message = SMSMessage(
        source_addr='1234',
        destination_addr='5678',
        message_text='Hello @ €',
        protocol='http',
    )

    await server.start()
    try:
        await engine._process_message(message)
    finally:
        await porth_client.disconnect()
        await server.stop()

    assert len(received) == 1
    assert received[0].data_coding == 0
    assert received[0].get_message_text() == 'Hello @ €'
    assert message.status == MessageStatus.SENT
    assert message.protocol_data['smsc_message_ids'] == ['smsc-1']


@pytest.mark.asyncio
async def test_long_message_goes_out_in_parts():
    """Text longer than one SMS reaches the SMSC as UDH-concatenated submit_sm parts."""
    port = free_port()
    received = []
    server = SMPPServer(host='127.0.0.1', port=port, setup_signal_handlers=False)

    def on_message_received(server, session, pdu):
        received.append(pdu)
        return f'smsc-{len(received)}'

    server.on_message_received = on_message_received

    engine = DeliveryEngine(MessageQueue(), MessageStore(), Settings())
    porth_client = SMPPClient(
        SMPPClientConfig(host='127.0.0.1', port=port, system_id='porth', password='pw')
    )
    engine.smpp_client = porth_client
    message = SMSMessage(
        source_addr='1234',
        destination_addr='5678',
        message_text='a' * 200,
        protocol='http',
    )

    await server.start()
    try:
        await engine._process_message(message)
    finally:
        await porth_client.disconnect()
        await server.stop()

    assert len(received) == 2
    assert all(pdu.esm_class & 0x40 for pdu in received)  # UDHI set by smppai
    assert message.status == MessageStatus.SENT
    assert message.protocol_data['smsc_message_ids'] == ['smsc-1', 'smsc-2']


RECEIPT = (
    'id:smsc-1 sub:001 dlvrd:001 submit date:2609261200 done date:2609261201 '
    'stat:DELIVRD err:000 text:'
)


async def send_and_receipt(message: SMSMessage, store: MessageStore) -> None:
    """Send message to an smppai SMSC that answers 'smsc-1', then deliver its receipt."""
    port = free_port()
    server = SMPPServer(host='127.0.0.1', port=port, setup_signal_handlers=False)
    server.on_message_received = lambda server, session, pdu: 'smsc-1'

    handler = DLRHandler(store)
    engine = DeliveryEngine(MessageQueue(), store, Settings())
    porth_client = SMPPClient(
        SMPPClientConfig(host='127.0.0.1', port=port, system_id='porth', password='pw'),
        on_receipt=handler.on_receipt,
    )
    engine.smpp_client = porth_client
    store.add(message)

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
            while message.status == MessageStatus.SENT:
                await asyncio.sleep(0.01)
            await asyncio.gather(*handler._tasks)
    finally:
        await porth_client.disconnect()
        await handler.stop()
        await server.stop()


@pytest.mark.asyncio
async def test_receipt_marks_message_delivered():
    message = SMSMessage(
        source_addr='1234', destination_addr='5678', message_text='hi', protocol='http'
    )
    await send_and_receipt(message, MessageStore())
    assert message.status == MessageStatus.DELIVERED
    assert message.delivered_at is not None


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
        await send_and_receipt(message, MessageStore())
    finally:
        await dlr_server.close()
    assert message.status == MessageStatus.DELIVERED
    assert [dict(r.query) for r in requests] == [{'d': '1', 'f': 'smsc-1'}]


@pytest.mark.asyncio
async def test_mo_reaches_the_application_and_its_reply_goes_back():
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

    queue, store = MessageQueue(), MessageStore()
    handler = MOHandler(
        queue, store, MOConfig(url=str(app_server.make_url('/mo')) + '?k=%k&p=%p')
    )
    engine = DeliveryEngine(queue, store, Settings())
    porth_client = SMPPClient(
        SMPPClientConfig(host='127.0.0.1', port=port, system_id='porth', password='pw'),
        on_mo=handler.on_mo,
    )
    engine.smpp_client = porth_client

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

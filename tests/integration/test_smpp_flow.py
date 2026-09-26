"""Integration test: real submit_sm over TCP to an smppai test SMSC."""

import socket

import pytest
from smpp import SMPPServer

from porth.config.settings import Settings, SMPPClientConfig
from porth.core.delivery import DeliveryEngine
from porth.core.message import MessageStatus, SMSMessage
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

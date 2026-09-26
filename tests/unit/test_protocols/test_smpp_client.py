"""Unit tests for the SMPP client (smppai faked)."""

import asyncio

import pytest

from smpp.exceptions import SMPPMessageException
from smpp.gsm import make_parts

from porth.config.settings import SMPPClientConfig
from porth.core.exceptions import MessageError
from porth.core.message import SMSMessage
from porth.protocols.smpp import client as client_module
from porth.protocols.smpp.client import SMPPClient


class FakeSmppai:
    instances: list = []
    fail_connect = False
    submit_error: Exception | None = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.submits: list = []
        self.bound = False
        self.disconnected = False
        FakeSmppai.instances.append(self)

    async def connect(self):
        await asyncio.sleep(0)  # yield so concurrent sends can interleave
        if FakeSmppai.fail_connect:
            raise ConnectionError('refused')

    async def bind_transceiver(self):
        self.bound = True

    @property
    def is_bound(self):
        return self.bound and not self.disconnected

    async def disconnect(self):
        self.disconnected = True

    async def submit_multipart(self, source_addr, destination_addr, message, **kwargs):
        if FakeSmppai.submit_error:
            raise FakeSmppai.submit_error
        self.submits.append(
            dict(
                source_addr=source_addr,
                destination_addr=destination_addr,
                message=message,
                **kwargs,
            )
        )
        parts = make_parts(message, kwargs['data_coding'])
        return [f'smsc-{42 + i}' for i in range(len(parts))]


@pytest.fixture
def smpp(monkeypatch):
    FakeSmppai.instances = []
    FakeSmppai.fail_connect = False
    FakeSmppai.submit_error = None
    monkeypatch.setattr(client_module, 'SmppaiClient', FakeSmppai)
    config = SMPPClientConfig(host='h', port=1, system_id='s', password='p')
    return SMPPClient(config)


def msg(text: str, dlr: bool = True) -> SMSMessage:
    return SMSMessage(
        source_addr='A',
        destination_addr='B',
        message_text=text,
        protocol='http',
        dlr_requested=dlr,
    )


@pytest.mark.asyncio
async def test_gsm_text_uses_default_coding(smpp):
    result = await smpp.send_message(msg('Hello @ €'))
    submit = FakeSmppai.instances[0].submits[0]
    assert submit['data_coding'] == 0
    assert submit['registered_delivery'] == 1
    assert submit['message'] == 'Hello @ €'
    assert result['smsc_message_ids'] == ['smsc-42']
    assert FakeSmppai.instances[0].bound


@pytest.mark.asyncio
async def test_addresses_carry_smppai_ton_npi(smpp):
    message = msg('hi')
    message.source_addr, message.destination_addr = 'ACME', '+48 600-100-200'
    await smpp.send_message(message)
    submit = FakeSmppai.instances[0].submits[0]
    assert (submit['source_addr'], submit['source_addr_ton']) == ('ACME', 5)
    assert submit['destination_addr'] == '48600100200'
    assert (submit['dest_addr_ton'], submit['dest_addr_npi']) == (1, 1)


@pytest.mark.asyncio
async def test_non_gsm_text_uses_ucs2(smpp):
    await smpp.send_message(msg('Привет', dlr=False))
    submit = FakeSmppai.instances[0].submits[0]
    assert submit['data_coding'] == 8
    assert submit['registered_delivery'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['a' * 160, 'Ж' * 70])
async def test_one_segment_fits(smpp, text):
    await smpp.send_message(msg(text))


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['a' * 161, 'Ж' * 71, '€' * 81])
async def test_long_text_is_sent_in_parts(smpp, text):
    result = await smpp.send_message(msg(text))
    assert result['smsc_message_ids'] == ['smsc-42', 'smsc-43']
    assert FakeSmppai.instances[0].submits[0]['message'] == text


@pytest.mark.asyncio
async def test_more_than_255_parts_fails_before_network(smpp):
    with pytest.raises(MessageError):
        await smpp.send_message(msg('a' * (153 * 255 + 1)))
    assert FakeSmppai.instances == []


@pytest.mark.asyncio
async def test_failed_part_reraises_and_smsc_rejection_keeps_the_bind(smpp, caplog):
    await smpp.connect()
    error = SMPPMessageException('rejected', command_status=0x45)
    error.sent_message_ids = ['smsc-1']
    FakeSmppai.submit_error = error
    with pytest.raises(SMPPMessageException):
        await smpp.send_message(msg('a' * 400))
    assert smpp.connected
    assert "['smsc-1']" in caplog.text


@pytest.mark.asyncio
async def test_failed_submit_drops_bind_and_next_send_rebinds(smpp):
    await smpp.connect()
    first = FakeSmppai.instances[0]
    FakeSmppai.submit_error = RuntimeError('boom')
    with pytest.raises(RuntimeError):
        await smpp.send_message(msg('hi'))
    assert smpp.connected is False
    assert first.disconnected

    FakeSmppai.submit_error = None
    await smpp.send_message(msg('hi'))
    assert len(FakeSmppai.instances) == 2
    assert FakeSmppai.instances[1].submits


@pytest.mark.asyncio
async def test_failed_connect_raises_and_next_send_retries(smpp):
    FakeSmppai.fail_connect = True
    with pytest.raises(ConnectionError):
        await smpp.send_message(msg('hi'))
    assert smpp.connected is False

    FakeSmppai.fail_connect = False
    await smpp.send_message(msg('hi'))
    assert len(FakeSmppai.instances) == 2
    assert smpp.connected


@pytest.mark.asyncio
async def test_concurrent_sends_open_one_bind(smpp):
    await asyncio.gather(smpp.send_message(msg('a')), smpp.send_message(msg('b')))
    assert len(FakeSmppai.instances) == 1
    assert len(FakeSmppai.instances[0].submits) == 2


@pytest.mark.asyncio
async def test_smsc_rejection_keeps_the_bind(smpp):
    await smpp.connect()
    FakeSmppai.submit_error = SMPPMessageException('rejected', command_status=0x0B)
    with pytest.raises(SMPPMessageException):
        await smpp.send_message(msg('hi'))
    assert smpp.connected
    assert not FakeSmppai.instances[0].disconnected


@pytest.mark.asyncio
async def test_unbind_from_smsc_rebinds_on_next_send(smpp):
    await smpp.connect()
    first = FakeSmppai.instances[0]
    first.bound = False  # smppai clears _bound on an SMSC unbind or lost connection
    await smpp.send_message(msg('hi'))
    assert first.disconnected
    assert len(FakeSmppai.instances) == 2
    assert FakeSmppai.instances[1].submits


@pytest.mark.asyncio
async def test_startup_connect_racing_a_send_opens_one_bind(smpp):
    await asyncio.gather(smpp.connect(), smpp.send_message(msg('a')))
    assert len(FakeSmppai.instances) == 1


@pytest.mark.asyncio
async def test_cancelled_bind_is_closed(smpp, monkeypatch):
    bind_started = asyncio.Event()

    async def slow_bind(self):
        bind_started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(FakeSmppai, 'bind_transceiver', slow_bind)
    send = asyncio.create_task(smpp.send_message(msg('hi')))
    await bind_started.wait()
    send.cancel()
    with pytest.raises(asyncio.CancelledError):
        await send
    assert FakeSmppai.instances[0].disconnected
    assert smpp.client is None

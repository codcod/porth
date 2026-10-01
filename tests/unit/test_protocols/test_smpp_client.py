"""Unit tests for the SMPP client (smppai faked)."""

import asyncio

import pytest

from smpp import CommandStatus, DeliverSm
from smpp.client import highlevel as smpp_highlevel
from smpp.exceptions import SMPPMessageException
from smpp.gsm import make_parts

from porth.config.settings import SMPPClientConfig
from porth.core.exceptions import MessageError
from porth.core.message import SMSMessage
from porth.protocols.smpp import client as client_module
from porth.protocols.smpp.client import Pacer, SMPPClient


class FakeSmppai:
    instances: list = []
    fail_connect = False
    submit_error: Exception | None = None

    def __init__(self, host=None, port=None, system_id=None, password=None, **kwargs):
        self.kwargs = dict(
            host=host, port=port, system_id=system_id, password=password, **kwargs
        )
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
    # smpp.connect() builds its raw client from this name; the real Client wraps it
    monkeypatch.setattr(smpp_highlevel, 'SMPPClient', FakeSmppai)
    config = SMPPClientConfig(host='h', port=1, system_id='s', password='p')
    return SMPPClient('s', config)


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
async def test_failed_connect_raises_and_next_send_retries(smpp, caplog):
    FakeSmppai.fail_connect = True
    with pytest.raises(ConnectionError):
        await smpp.send_message(msg('hi'))
    assert smpp.connected is False
    assert 'SMSC s: Failed to connect' in caplog.text

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
async def test_invalid_bind_status_drops_the_bind(smpp):
    await smpp.connect()
    FakeSmppai.submit_error = SMPPMessageException(
        'not bound', command_status=CommandStatus.ESME_RINVBNDSTS
    )
    with pytest.raises(SMPPMessageException):
        await smpp.send_message(msg('hi'))
    assert smpp.connected is False
    assert FakeSmppai.instances[0].disconnected


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


def deliver_sm(text: str, esm_class: int) -> DeliverSm:
    return DeliverSm(
        source_addr='5678',
        destination_addr='1234',
        short_message=text.encode(),
        esm_class=esm_class,
    )


def recorder(into: list):
    """An on_receipt that records each receipt (awaited, as DLRHandler's is)."""

    async def on_receipt(m):
        into.append(m)

    return on_receipt


@pytest.mark.asyncio
async def test_receipts_reach_on_receipt_and_mo_reaches_on_mo(smpp):
    receipts, mos = [], []
    smpp.on_receipt, smpp.on_mo = recorder(receipts), mos.append
    await smpp.connect()
    fake = FakeSmppai.instances[0]
    fake.on_deliver_sm(fake, deliver_sm('hello', 0))
    fake.on_deliver_sm(fake, deliver_sm('id:smsc-42 stat:DELIVRD err:000', 0x04))
    for _ in range(3):
        await asyncio.sleep(0)
    assert [m.receipt.id for m in receipts] == ['smsc-42']
    assert [(m.text, m.sender.addr) for m in mos] == [('hello', '5678')]
    await smpp.disconnect()


@pytest.mark.asyncio
async def test_failing_on_mo_is_logged_and_the_loop_goes_on(smpp, caplog):
    mos = []

    def on_mo(m):
        mos.append(m)
        raise RuntimeError('boom')

    smpp.on_mo = on_mo
    await smpp.connect()
    fake = FakeSmppai.instances[0]
    fake.on_deliver_sm(fake, deliver_sm('one', 0))
    fake.on_deliver_sm(fake, deliver_sm('two', 0))
    for _ in range(3):
        await asyncio.sleep(0)
    assert [m.text for m in mos] == ['one', 'two']
    assert 'Error handling MO' in caplog.text
    await smpp.disconnect()


@pytest.mark.asyncio
async def test_lost_connection_ends_the_consumer_quietly(smpp):
    await smpp.connect()
    fake, inbound = FakeSmppai.instances[0], smpp._inbound
    fake.on_connection_lost(fake, ConnectionError('gone'))
    await inbound
    assert inbound.exception() is None


async def rebind_unbound(smpp, fake):
    fake.bound = False
    await smpp.connect()


async def failed_submit(smpp, fake):
    FakeSmppai.submit_error = RuntimeError('boom')
    with pytest.raises(RuntimeError):
        await smpp.send_message(msg('hi'))


async def disconnect(smpp, fake):
    await smpp.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize('drop', [disconnect, failed_submit, rebind_unbound])
async def test_dropping_a_bind_drains_its_receipts(smpp, drop):
    receipts = []
    smpp.on_receipt = recorder(receipts)
    await smpp.connect()
    fake, consumer = FakeSmppai.instances[0], smpp._inbound
    real_disconnect = fake.disconnect

    async def disconnect_with_a_late_receipt():
        fake.on_deliver_sm(fake, deliver_sm('id:smsc-43 stat:DELIVRD err:000', 4))
        await real_disconnect()

    fake.disconnect = disconnect_with_a_late_receipt
    # Queued in smppai's Client, not yet consumed
    fake.on_deliver_sm(fake, deliver_sm('id:smsc-42 stat:DELIVRD err:000', 4))
    await drop(smpp, fake)
    assert [m.receipt.id for m in receipts] == ['smsc-42', 'smsc-43']
    assert consumer.done() and not consumer.cancelled()
    assert fake.disconnected


async def until(condition):
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError('condition never held')


def lose_connection(fake):
    fake.bound = False  # smppai clears it, then reports the loss
    fake.on_connection_lost(fake, ConnectionError('gone'))


def smsc_unbind(fake):
    fake.bound = False  # the socket stays open; messages() does not end


def startup_bind_failed(fake):
    FakeSmppai.fail_connect = False  # the SMSC comes back


@pytest.mark.asyncio
@pytest.mark.parametrize('outage', [lose_connection, smsc_unbind, startup_bind_failed])
async def test_rebind_loop_rebinds_without_sends(smpp, monkeypatch, outage):
    monkeypatch.setattr(client_module, '_REBIND_DELAY', 0.01)
    FakeSmppai.fail_connect = outage is startup_bind_failed
    smpp.start()
    await until(lambda: FakeSmppai.instances)
    first = FakeSmppai.instances[0]
    outage(first)
    await until(lambda: smpp.connected and smpp.client is not first)
    assert all(not fake.submits for fake in FakeSmppai.instances)

    await smpp.disconnect()
    assert smpp._keeper is None and not smpp.connected
    created = len(FakeSmppai.instances)
    await asyncio.sleep(0.05)
    assert len(FakeSmppai.instances) == created


@pytest.mark.asyncio
async def test_disconnect_waits_for_a_drain_the_rebind_loop_left(smpp, monkeypatch):
    """POR-013 review F3: disconnect() cancels the rebind loop inside _drop(); the
    shielded drain goes on, and disconnect() must wait for it."""
    monkeypatch.setattr(client_module, '_REBIND_DELAY', 0.01)
    started, release, stored = asyncio.Event(), asyncio.Event(), []

    async def on_receipt(m):  # a slow database write
        started.set()
        await release.wait()
        stored.append(m.receipt.id)

    smpp.on_receipt = on_receipt
    smpp.start()
    await until(lambda: smpp.connected)
    fake = FakeSmppai.instances[0]
    fake.on_deliver_sm(fake, deliver_sm('id:smsc-42 stat:DELIVRD err:000', 4))
    await started.wait()
    fake.bound = False  # the rebind loop drops the bind and waits on the drain
    await until(lambda: fake.disconnected)
    await asyncio.sleep(0.02)

    stop = asyncio.create_task(smpp.disconnect())
    await asyncio.sleep(0.05)
    assert not stop.done()
    release.set()
    await stop
    assert stored == ['smsc-42']
    assert smpp._inbound is None


def test_pacer_spaces_sends_by_their_parts():
    pacer = Pacer(10)
    waits = [pacer.reserve(n, 0) for n in (1, 1, 3, 1)]
    assert waits == pytest.approx([0, 0.1, 0.2, 0.5])


def test_pacer_holds_the_window_for_a_late_multipart():
    pacer = Pacer(10)
    for _ in range(9):
        pacer.reserve(1, 0)
    # spacing alone says 0.9, which puts 12 PDUs in [0, 1)
    assert pacer.reserve(3, 0) == pytest.approx(1.1)


def test_pacer_holds_the_window_after_an_early_multipart():
    pacer = Pacer(10)
    pacer.reserve(3, 0)
    waits = [pacer.reserve(1, 0) for _ in range(8)]
    assert waits[-1] == pytest.approx(1.0)  # only 7 fit after the 3 in [0, 1)


def test_pacer_stores_no_burst_after_idle():
    pacer = Pacer(10)
    pacer.reserve(1, 0)
    assert pacer.reserve(1, 5) == 0
    assert pacer.reserve(1, 5) == pytest.approx(0.1)


def test_pacer_sends_a_message_over_the_cap_whole():
    pacer = Pacer(2)
    assert pacer.reserve(5, 0) == 0
    assert pacer.reserve(1, 0) == pytest.approx(2.5)


class SpyPacer:
    def __init__(self, smpp):
        self.smpp = smpp
        self.calls: list = []

    def reserve(self, n, now):
        self.calls.append((n, self.smpp.connected))
        return 0


@pytest.mark.asyncio
async def test_send_paces_after_the_bind_per_part(smpp):
    smpp._pacer = spy = SpyPacer(smpp)
    await smpp.send_message(msg('a' * 200))  # GSM 03.38: two parts
    await smpp.send_message(msg('ж' * 71))  # UCS2: two parts
    assert spy.calls == [(2, True), (2, True)]


@pytest.mark.asyncio
async def test_bind_lost_while_pacing_rebinds_and_books_a_fresh_turn(smpp):
    smpp._pacer = spy = SpyPacer(smpp)
    await smpp.connect()
    first = smpp.client
    real_reserve = spy.reserve

    def drop_on_first(n, now):
        if not spy.calls:
            first.disconnected = True  # the bind goes while this send waits
        return real_reserve(n, now)

    spy.reserve = drop_on_first
    await smpp.send_message(msg('hi'))
    assert first.submits == []
    assert smpp.client is not first and len(smpp.client.submits) == 1
    assert len(spy.calls) == 2


@pytest.mark.asyncio
async def test_failed_connect_takes_no_turn(smpp):
    smpp._pacer = spy = SpyPacer(smpp)
    FakeSmppai.fail_connect = True
    with pytest.raises(ConnectionError):
        await smpp.send_message(msg('hi'))
    assert spy.calls == []


def test_throughput_below_one_fails_and_unset_builds_no_pacer(smpp):
    config = SMPPClientConfig(
        host='h', port=1, system_id='s', password='p', throughput=0
    )
    with pytest.raises(ValueError, match='porth.smsc.s.throughput'):
        SMPPClient('s', config)
    assert smpp._pacer is None

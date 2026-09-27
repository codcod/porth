"""Unit tests for the DLR handler (receipt correlation and Kannel dlr-url callbacks)."""

import asyncio
import types

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from smpp import DeliveryReceipt, MessageState

from porth.core import dlr as dlr_module
from porth.core.dlr import DLRHandler, expand_url
from porth.core.message import MessageStatus, SMSMessage
from porth.core.store import MessageStore


def receipt(smsc_id, state, text='id:x stat:DELIVRD'):
    return types.SimpleNamespace(
        receipt=DeliveryReceipt(id=smsc_id, state=state), text=text, is_receipt=True
    )


def sent(store, ids, protocol='http', dlr_url=None, dlr_mask=0):
    message = SMSMessage(
        source_addr='A',
        destination_addr='B',
        message_text='hi',
        protocol=protocol,
        dlr_url=dlr_url,
        protocol_data={'dlr_mask': dlr_mask, 'smsc_message_ids': ids},
        status=MessageStatus.SENT,
    )
    store.add(message)
    store.add_smsc_ids(message, ids)
    return message


@pytest.fixture
def store():
    return MessageStore()


def test_two_parts_deliver_only_when_both_do(store):
    handler = DLRHandler(store)
    message = sent(store, ['s1', 's2'])
    handler.on_receipt(receipt('s1', MessageState.DELIVERED))
    assert message.status == MessageStatus.SENT
    handler.on_receipt(receipt('s2', MessageState.DELIVERED))
    assert message.status == MessageStatus.DELIVERED
    assert message.delivered_at is not None and message.delivered_at.tzinfo is None


@pytest.mark.parametrize('part', ['s1', 's2'])
@pytest.mark.parametrize(
    'state',
    [MessageState.UNDELIVERABLE, MessageState.REJECTED, MessageState.DELETED],
)
def test_failing_part_fails_the_message(store, part, state):
    message = sent(store, ['s1', 's2'])
    DLRHandler(store).on_receipt(receipt(part, state))
    assert message.status == MessageStatus.FAILED


@pytest.mark.parametrize(
    'state, status',
    [
        (MessageState.EXPIRED, MessageStatus.EXPIRED),
        (MessageState.ENROUTE, MessageStatus.SENT),
        (MessageState.ACCEPTED, MessageStatus.SENT),
        (MessageState.UNKNOWN, MessageStatus.SENT),
        (None, MessageStatus.SENT),
    ],
)
def test_state_mapping(store, state, status):
    message = sent(store, ['s1'])
    DLRHandler(store).on_receipt(receipt('s1', state))
    assert message.status == status


@pytest.mark.asyncio
async def test_unknown_id_and_terminal_message_are_ignored(store, handler, caplog):
    caplog.set_level('INFO')
    message = sent(store, ['s1'])
    handler.on_receipt(receipt('nope', MessageState.DELIVERED))
    handler.on_receipt(receipt(None, MessageState.DELIVERED))
    assert 'unknown SMSC id None ignored' in caplog.text
    assert "'nope'" not in caplog.text  # still being looked up
    await settle(handler)
    assert "unknown SMSC id 'nope' ignored" in caplog.text
    assert message.status == MessageStatus.SENT
    handler.on_receipt(receipt('s1', MessageState.UNDELIVERABLE))
    handler.on_receipt(receipt('s1', MessageState.DELIVERED))
    assert message.status == MessageStatus.FAILED


@pytest.mark.asyncio
async def test_early_part_receipt_counts_once_its_id_is_indexed(
    store, handler, recorder, monkeypatch
):
    monkeypatch.setattr(dlr_module, '_UNKNOWN_WAITS', (0.001, 0.001))
    message = SMSMessage(
        source_addr='A',
        destination_addr='B',
        message_text='hi',
        protocol='kannel',
        dlr_url=recorder.url(),
        protocol_data={'dlr_mask': 1, 'smsc_message_ids': ['s1', 's2']},
        status=MessageStatus.SENT,
    )
    store.add(message)
    # Part 1's receipt beats part 2's submit_sm_resp, so its id is not indexed yet
    handler.on_receipt(receipt('s1', MessageState.DELIVERED))
    store.add_smsc_ids(message, ['s1', 's2'])
    handler.on_receipt(receipt('s2', MessageState.DELIVERED))
    assert message.status == MessageStatus.SENT
    await settle(handler)
    assert message.status == MessageStatus.DELIVERED
    assert len(recorder.requests) == 1


@pytest.mark.asyncio
async def test_stop_cancels_a_pending_recheck(store, monkeypatch):
    monkeypatch.setattr(dlr_module, '_UNKNOWN_WAITS', (3600,))
    handler = DLRHandler(store)
    await handler.start()
    handler.on_receipt(receipt('s1', MessageState.DELIVERED))
    (recheck,) = handler._tasks
    await handler.stop()
    assert recheck.cancelled()
    message = sent(store, ['s1'])
    await asyncio.sleep(0)
    assert message.status == MessageStatus.SENT


def test_expand_url_is_single_pass_and_quotes():
    url = 'http://x/?id=%I&s=%d&f=%F&a=%A&t=%t&T=%T&p=%p&s2=%s'
    values = {
        'I': 'm-1',
        'd': '1',
        'F': 'a&b',
        'A': 'id:1 stat:DELIVRD %d',
        't': '2026-09-26 12:00',
        'T': '1790424000',
    }
    assert expand_url(url, values) == (
        'http://x/?id=m-1&s=1&f=a%26b&a=id%3A1%20stat%3ADELIVRD%20%25d'
        '&t=2026-09-26%2012%3A00&T=1790424000&p=%p&s2=%s'
    )


def test_expand_url_keeps_kannel_percent_rules():
    url = 'a=%%d&b=%d&c=caf%%C3%%A9&e=%x&f=%'
    assert expand_url(url, {'d': '1'}) == 'a=%d&b=1&c=caf%C3%A9&e=%x&f=%'


class Recorder:
    """A local dlr-url server that answers with the queued statuses, then 200."""

    def __init__(self, statuses=()):
        self.requests: list[web.Request] = []
        self.statuses = list(statuses)
        app = web.Application()
        app.router.add_route('*', '/dlr', self.handle)
        self.server = TestServer(app)

    async def handle(self, request):
        self.requests.append(request)
        return web.Response(status=self.statuses.pop(0) if self.statuses else 200)

    def url(self, query='d=%d&id=%I&f=%F'):
        return str(self.server.make_url('/dlr')) + '?' + query


@pytest_asyncio.fixture
async def recorder():
    recorder = Recorder()
    await recorder.server.start_server()
    yield recorder
    await recorder.server.close()


@pytest_asyncio.fixture
async def handler(store, monkeypatch):
    real_sleep = asyncio.sleep

    async def no_sleep(_):
        await real_sleep(0)

    monkeypatch.setattr(dlr_module.asyncio, 'sleep', no_sleep)
    handler = DLRHandler(store)
    await handler.start()
    yield handler
    await handler.stop()


async def settle(handler):
    while handler._tasks:  # a re-check can start a dlr-url fetch
        await asyncio.gather(*handler._tasks)


@pytest.mark.asyncio
async def test_kannel_mask_3_delivered_gets_one_get(store, handler, recorder):
    message = sent(store, ['s1'], 'kannel', recorder.url(), dlr_mask=3)
    handler.on_receipt(receipt('s1', MessageState.DELIVERED))
    await settle(handler)
    assert len(recorder.requests) == 1
    request = recorder.requests[0]
    assert request.method == 'GET'
    assert dict(request.query) == {'d': '1', 'id': message.message_id, 'f': 's1'}


@pytest.mark.asyncio
async def test_kannel_failed_reports_2(store, handler, recorder):
    sent(store, ['s1'], 'kannel', recorder.url(), dlr_mask=2)
    handler.on_receipt(receipt('s1', MessageState.EXPIRED))
    await settle(handler)
    assert [r.query['d'] for r in recorder.requests] == ['2']


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol, mask', [('kannel', 2), ('kannel', 0), ('http', 3)])
async def test_no_callback(store, handler, recorder, protocol, mask):
    sent(store, ['s1'], protocol, recorder.url(), dlr_mask=mask)
    handler.on_receipt(receipt('s1', MessageState.DELIVERED))
    await settle(handler)
    assert recorder.requests == []


@pytest.mark.asyncio
async def test_multipart_calls_back_once(store, handler, recorder):
    sent(store, ['s1', 's2'], 'kannel', recorder.url(), dlr_mask=1)
    handler.on_receipt(receipt('s1', MessageState.DELIVERED))
    handler.on_receipt(receipt('s2', MessageState.DELIVERED))
    handler.on_receipt(receipt('s2', MessageState.DELIVERED))
    await settle(handler)
    assert [r.query['f'] for r in recorder.requests] == ['s2']


@pytest.mark.asyncio
async def test_retries_until_2xx(store, handler, recorder):
    recorder.statuses = [500, 500]
    sent(store, ['s1'], 'kannel', recorder.url(), dlr_mask=1)
    handler.on_receipt(receipt('s1', MessageState.DELIVERED))
    await settle(handler)
    assert len(recorder.requests) == 3


@pytest.mark.asyncio
async def test_gives_up_after_three_attempts(store, handler, recorder):
    recorder.statuses = [500] * 5
    sent(store, ['s1'], 'kannel', recorder.url(), dlr_mask=1)
    handler.on_receipt(receipt('s1', MessageState.DELIVERED))
    await settle(handler)
    assert len(recorder.requests) == 3


@pytest.mark.asyncio
async def test_stop_without_start_is_a_no_op(store):
    await DLRHandler(store).stop()

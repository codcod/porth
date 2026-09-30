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


def receipt(smsc_id, state, text='id:x stat:DELIVRD'):
    return types.SimpleNamespace(
        receipt=DeliveryReceipt(id=smsc_id, state=state), text=text, is_receipt=True
    )


def sent(repo, ids, protocol='http', dlr_url=None, dlr_mask=0):
    """A sent message in the repository; returns a reader of its stored row."""
    message = SMSMessage(
        source_addr='A',
        destination_addr='B',
        message_text='hi',
        protocol=protocol,
        dlr_url=dlr_url,
        protocol_data={'dlr_mask': dlr_mask, 'smsc_message_ids': ids},
        status=MessageStatus.SENT,
        smsc='a',
    )
    repo.messages[message.message_id] = message
    repo.smsc_ids.update(dict.fromkeys((('a', i) for i in ids), message.message_id))
    return lambda: repo.messages[message.message_id]


@pytest.fixture
def store(uow_factory):
    return uow_factory.repo


@pytest.mark.asyncio
async def test_two_parts_deliver_only_when_both_do(store, uow_factory):
    handler = DLRHandler(uow_factory)
    message = sent(store, ['s1', 's2'])
    await handler.on_receipt(receipt('s1', MessageState.DELIVERED), 'a')
    assert message().status == MessageStatus.SENT
    assert message().protocol_data['delivered_smsc_ids'] == ['s1']  # JSON: a list
    await handler.on_receipt(receipt('s2', MessageState.DELIVERED), 'a')
    assert message().status == MessageStatus.DELIVERED
    assert message().delivered_at is not None
    assert message().delivered_at.tzinfo is not None


@pytest.mark.parametrize('part', ['s1', 's2'])
@pytest.mark.parametrize(
    'state',
    [MessageState.UNDELIVERABLE, MessageState.REJECTED, MessageState.DELETED],
)
@pytest.mark.asyncio
async def test_failing_part_fails_the_message(store, uow_factory, part, state):
    message = sent(store, ['s1', 's2'])
    await DLRHandler(uow_factory).on_receipt(receipt(part, state), 'a')
    assert message().status == MessageStatus.FAILED


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
@pytest.mark.asyncio
async def test_state_mapping(store, uow_factory, state, status):
    message = sent(store, ['s1'])
    await DLRHandler(uow_factory).on_receipt(receipt('s1', state), 'a')
    assert message().status == status


@pytest.mark.asyncio
async def test_unknown_id_and_terminal_message_are_ignored(store, handler, caplog):
    caplog.set_level('INFO')
    message = sent(store, ['s1'])
    await handler.on_receipt(receipt('nope', MessageState.DELIVERED), 'a')
    await handler.on_receipt(receipt(None, MessageState.DELIVERED), 'a')
    assert 'unknown SMSC id None from a ignored' in caplog.text
    assert "'nope'" not in caplog.text  # still being looked up
    await settle(handler)
    assert "unknown SMSC id 'nope' from a ignored" in caplog.text
    assert message().status == MessageStatus.SENT
    await handler.on_receipt(receipt('s1', MessageState.UNDELIVERABLE), 'a')
    await handler.on_receipt(receipt('s1', MessageState.DELIVERED), 'a')
    assert message().status == MessageStatus.FAILED


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
    store.messages[message.message_id] = message
    # Part 1's receipt beats part 2's submit_sm_resp, so its id is not indexed yet
    await handler.on_receipt(receipt('s1', MessageState.DELIVERED), 'a')
    store.smsc_ids.update(
        {('a', 's1'): message.message_id, ('a', 's2'): message.message_id}
    )
    await handler.on_receipt(receipt('s2', MessageState.DELIVERED), 'a')
    assert store.messages[message.message_id].status == MessageStatus.SENT
    await settle(handler)
    assert store.messages[message.message_id].status == MessageStatus.DELIVERED
    assert len(recorder.requests) == 1


@pytest.mark.asyncio
async def test_stop_cancels_a_pending_recheck(store, uow_factory, monkeypatch):
    monkeypatch.setattr(dlr_module, '_UNKNOWN_WAITS', (3600,))
    handler = DLRHandler(uow_factory)
    await handler.start()
    await handler.on_receipt(receipt('s1', MessageState.DELIVERED), 'a')
    (recheck,) = handler._tasks
    await handler.stop()
    assert recheck.cancelled()
    message = sent(store, ['s1'])
    await asyncio.sleep(0)
    assert message().status == MessageStatus.SENT


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
async def handler(uow_factory, monkeypatch):
    real_sleep = asyncio.sleep

    async def no_sleep(_):
        await real_sleep(0)

    monkeypatch.setattr(dlr_module.asyncio, 'sleep', no_sleep)
    handler = DLRHandler(uow_factory)
    await handler.start()
    yield handler
    await handler.stop()


async def settle(handler):
    while handler._tasks:  # a re-check can start a dlr-url fetch
        await asyncio.gather(*handler._tasks)


@pytest.mark.asyncio
async def test_kannel_mask_3_delivered_gets_one_get(store, handler, recorder):
    message = sent(store, ['s1'], 'kannel', recorder.url(), dlr_mask=3)
    await handler.on_receipt(receipt('s1', MessageState.DELIVERED), 'a')
    await settle(handler)
    assert len(recorder.requests) == 1
    request = recorder.requests[0]
    assert request.method == 'GET'
    assert dict(request.query) == {'d': '1', 'id': message().message_id, 'f': 's1'}


@pytest.mark.asyncio
async def test_same_id_from_another_smsc_is_not_this_message(store, handler, caplog):
    caplog.set_level('INFO')
    message = sent(store, ['s1'])  # from SMSC a
    await handler.on_receipt(receipt('s1', MessageState.DELIVERED), 'b')
    await settle(handler)  # the re-checks look for ('b', 's1') too
    assert "unknown SMSC id 's1' from b ignored" in caplog.text
    assert message().status == MessageStatus.SENT


@pytest.mark.asyncio
async def test_dlr_url_percent_i_is_the_smsc(store, handler, recorder):
    sent(store, ['s1'], 'kannel', recorder.url('i=%i'), dlr_mask=1)
    await handler.on_receipt(receipt('s1', MessageState.DELIVERED), 'a')
    await settle(handler)
    assert [dict(r.query) for r in recorder.requests] == [{'i': 'a'}]


@pytest.mark.asyncio
async def test_kannel_failed_reports_2(store, handler, recorder):
    sent(store, ['s1'], 'kannel', recorder.url(), dlr_mask=2)
    await handler.on_receipt(receipt('s1', MessageState.EXPIRED), 'a')
    await settle(handler)
    assert [r.query['d'] for r in recorder.requests] == ['2']


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol, mask', [('kannel', 2), ('kannel', 0), ('http', 3)])
async def test_no_callback(store, handler, recorder, protocol, mask):
    sent(store, ['s1'], protocol, recorder.url(), dlr_mask=mask)
    await handler.on_receipt(receipt('s1', MessageState.DELIVERED), 'a')
    await settle(handler)
    assert recorder.requests == []


@pytest.mark.asyncio
async def test_multipart_calls_back_once(store, handler, recorder):
    sent(store, ['s1', 's2'], 'kannel', recorder.url(), dlr_mask=1)
    await handler.on_receipt(receipt('s1', MessageState.DELIVERED), 'a')
    await handler.on_receipt(receipt('s2', MessageState.DELIVERED), 'a')
    await handler.on_receipt(receipt('s2', MessageState.DELIVERED), 'a')
    await settle(handler)
    assert [r.query['f'] for r in recorder.requests] == ['s2']


@pytest.mark.asyncio
async def test_retries_until_2xx(store, handler, recorder):
    recorder.statuses = [500, 500]
    sent(store, ['s1'], 'kannel', recorder.url(), dlr_mask=1)
    await handler.on_receipt(receipt('s1', MessageState.DELIVERED), 'a')
    await settle(handler)
    assert len(recorder.requests) == 3


@pytest.mark.asyncio
async def test_gives_up_after_three_attempts(store, handler, recorder):
    recorder.statuses = [500] * 5
    sent(store, ['s1'], 'kannel', recorder.url(), dlr_mask=1)
    await handler.on_receipt(receipt('s1', MessageState.DELIVERED), 'a')
    await settle(handler)
    assert len(recorder.requests) == 3


@pytest.mark.asyncio
async def test_stop_without_start_is_a_no_op(uow_factory):
    await DLRHandler(uow_factory).stop()


@pytest.mark.asyncio
async def test_receipt_stores_status_and_callback_before_the_fetch(
    store, uow_factory, handler, recorder, monkeypatch
):
    seen = []
    real_call = handler._call

    async def call(message_id, url):
        seen.append((store.messages[message_id].status, dict(store.dlr_callbacks)))
        await real_call(message_id, url)

    monkeypatch.setattr(handler, '_call', call)
    message = sent(store, ['s1'], 'kannel', recorder.url(), dlr_mask=1)
    await handler.on_receipt(receipt('s1', MessageState.DELIVERED), 'a')
    assert uow_factory.uows[0].committed
    await settle(handler)
    ((status, callbacks),) = seen
    assert status == MessageStatus.DELIVERED
    assert list(callbacks) == [message().message_id]
    assert callbacks[message().message_id].startswith(recorder.url().split('?')[0])


@pytest.mark.asyncio
@pytest.mark.parametrize('statuses', [[], [500] * 5], ids=['success', 'give-up'])
async def test_fetch_forgets_the_stored_call_when_done(
    store, handler, recorder, statuses
):
    recorder.statuses = statuses
    sent(store, ['s1'], 'kannel', recorder.url(), dlr_mask=1)
    await handler.on_receipt(receipt('s1', MessageState.DELIVERED), 'a')
    assert len(store.dlr_callbacks) == 1
    await settle(handler)
    assert store.dlr_callbacks == {}


@pytest.mark.asyncio
async def test_stopped_fetch_stays_stored_and_resume_makes_it(
    store, uow_factory, recorder
):
    recorder.statuses = [500]  # the first attempt fails, then the handler stops
    handler = DLRHandler(uow_factory)
    await handler.start()
    sent(store, ['s1'], 'kannel', recorder.url(), dlr_mask=1)
    await handler.on_receipt(receipt('s1', MessageState.DELIVERED), 'a')
    async with asyncio.timeout(5):
        while not recorder.requests:
            await asyncio.sleep(0.01)
    await handler.stop()
    assert len(store.dlr_callbacks) == 1

    handler = DLRHandler(uow_factory)  # the next run
    await handler.start()
    await handler.resume()
    await settle(handler)
    await handler.stop()
    assert len(recorder.requests) == 2
    assert store.dlr_callbacks == {}


@pytest.mark.asyncio
async def test_database_error_is_logged_and_swallowed(
    store, uow_factory, handler, caplog
):
    sent(store, ['s1'])
    uow_factory.fail = OSError('database down')
    await handler.on_receipt(receipt('s1', MessageState.DELIVERED), 'a')
    assert "Receipt 's1'" in caplog.text and 'not saved' in caplog.text
    assert not handler._tasks  # no re-check, no fetch

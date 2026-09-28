"""Unit tests for DeliveryEngine._process_message."""

import asyncio
import copy

import pytest
from smpp import CommandStatus
from smpp.exceptions import SMPPBindException, SMPPMessageException

from porth.config.settings import DeliveryConfig, Settings
from porth.core import delivery as delivery_module
from porth.core.delivery import DeliveryEngine, retry_delay
from porth.core.exceptions import MessageError
from porth.core.message import MessageStatus, SMSMessage
from porth.core.queue import MessageQueue
from tests.conftest import FakeUowFactory


class FakeHandler:
    def __init__(self, error: Exception | None = None):
        self.error = error

    async def send_message(self, message):
        if self.error:
            raise self.error
        return {'smsc_message_ids': ['smsc-7', 'smsc-8']}


class Engine(DeliveryEngine):
    """The engine plus the fake repository its unit of work writes to."""

    def stored(self, message):
        return self.uow_factory.repo.messages[message.message_id]


def make(handler=None, settings=None, uow_factory=None):
    uow_factory = uow_factory or FakeUowFactory()
    engine = Engine(MessageQueue(), uow_factory, settings or Settings())  # 3 retries
    engine.smpp_client = handler
    message = SMSMessage(
        source_addr='A',
        destination_addr='B',
        message_text='hi',
        protocol='http',
        status=MessageStatus.QUEUED,  # as taken off the queue
    )
    uow_factory.repo.messages[message.message_id] = copy.deepcopy(message)
    return engine, message


@pytest.mark.asyncio
async def test_success_marks_sent_with_smsc_id():
    engine, message = make(FakeHandler())
    await engine._process_message(message)
    assert message.status == MessageStatus.SENT
    assert message.sent_at is not None
    assert message.protocol_data['smsc_message_ids'] == ['smsc-7', 'smsc-8']
    stored = engine.stored(message)
    assert stored.status == MessageStatus.SENT
    assert stored.sent_at == message.sent_at and stored.sent_at.tzinfo is not None
    assert stored.protocol_data['smsc_message_ids'] == ['smsc-7', 'smsc-8']
    assert engine.uow_factory.repo.smsc_ids == {
        'smsc-7': message.message_id,
        'smsc-8': message.message_id,
    }
    assert engine.uow_factory.uows[-1].committed


class CountingHandler(FakeHandler):
    def __init__(self):
        super().__init__()
        self.sends = 0

    async def send_message(self, message):
        self.sends += 1
        return await super().send_message(message)


class FlakyUowFactory(FakeUowFactory):
    """Commits fail for the first `failures` units of work, then succeed."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    def __call__(self):
        self.fail = OSError('database down') if self.failures > 0 else None
        self.failures -= 1
        return super().__call__()


@pytest.mark.asyncio
async def test_failing_sent_write_is_retried_not_resent(monkeypatch):
    monkeypatch.setattr(delivery_module, '_SENT_WRITE_WAITS', (0, 0, 0))
    handler = CountingHandler()
    engine, message = make(handler, uow_factory=FlakyUowFactory(failures=2))
    await engine._process_message(message)
    assert handler.sends == 1
    assert len(engine.uow_factory.uows) == 3
    assert engine.stored(message).status == MessageStatus.SENT
    assert engine.uow_factory.repo.smsc_ids == {
        'smsc-7': message.message_id,
        'smsc-8': message.message_id,
    }


@pytest.mark.asyncio
async def test_failing_sent_write_is_logged_not_resent(caplog, monkeypatch):
    monkeypatch.setattr(delivery_module, '_SENT_WRITE_WAITS', (0, 0, 0))
    handler = CountingHandler()
    engine, message = make(handler)
    engine.uow_factory.fail = OSError('database down')
    await engine._process_message(message)
    assert handler.sends == 1
    assert len(engine.uow_factory.uows) == 4  # the first write and three retries
    assert message.status == MessageStatus.SENT
    assert message.retry_count == 0 and not engine._retries
    assert engine.message_queue.empty()
    assert f'{message.message_id} sent as' in caplog.text
    # Rolled back: the row keeps its old status and no SMSC id is indexed
    assert engine.stored(message).status == MessageStatus.QUEUED
    assert not engine.uow_factory.repo.smsc_ids


@pytest.mark.asyncio
async def test_message_error_fails_without_retry():
    engine, message = make(FakeHandler(MessageError('too long')))
    await engine._process_message(message)
    assert message.status == MessageStatus.FAILED
    assert message.retry_count == 0
    assert not engine._retries
    assert engine.stored(message).status == MessageStatus.FAILED


@pytest.mark.asyncio
async def test_no_client_goes_through_retry():
    engine, message = make(None)
    await engine._process_message(message)
    assert message.retry_count == 1
    assert message.status == MessageStatus.QUEUED
    assert len(engine._retries) == 1
    stored = engine.stored(message)
    assert (stored.status, stored.retry_count) == (MessageStatus.QUEUED, 1)


def test_retry_delay_backs_off_to_the_cap():
    config = DeliveryConfig(retry_delay=5, backoff_factor=2, max_retry_delay=30)
    assert [retry_delay(n, config) for n in range(1, 6)] == [5, 10, 20, 30, 30]
    config = DeliveryConfig(retry_delay=5, backoff_factor=1)
    assert [retry_delay(n, config) for n in range(1, 6)] == [5] * 5


@pytest.mark.asyncio
async def test_retries_wait_independently():
    engine, _ = make(settings=Settings(delivery=DeliveryConfig(retry_delay=1)))
    messages = [make(uow_factory=engine.uow_factory)[1] for _ in range(5)]
    for message in messages:
        await engine._handle_delivery_failure(message, 'down')
    async with asyncio.timeout(1.8):  # one shared retry worker needed ~5 s
        while engine.message_queue.qsize() < 5:
            await asyncio.sleep(0.05)
    assert not engine._retries


@pytest.mark.asyncio
async def test_permanent_smsc_rejection_fails_without_retry():
    error = SMPPMessageException('x', command_status=CommandStatus.ESME_RINVDESTADR)
    engine, message = make(FakeHandler(error))
    await engine._process_message(message)
    assert message.status == MessageStatus.FAILED
    assert message.retry_count == 0
    assert not engine._retries
    assert engine.stored(message).status == MessageStatus.FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'error',
    [
        SMPPMessageException('x', command_status=CommandStatus.ESME_RTHROTTLED),
        # a bind refusal is not a verdict on this message (review F1)
        SMPPBindException(
            'x', bind_type='trx', command_status=CommandStatus.ESME_RBINDFAIL
        ),
        # the SMSC lost the session: says nothing about this message (code review)
        SMPPMessageException('x', command_status=CommandStatus.ESME_RINVBNDSTS),
        OSError('reset'),
    ],
)
async def test_transient_failure_is_retried(error):
    engine, message = make(FakeHandler(error))
    await engine._process_message(message)
    assert message.retry_count == 1
    assert message.status == MessageStatus.QUEUED
    assert len(engine._retries) == 1


@pytest.mark.asyncio
async def test_last_attempt_fails_without_retry():
    engine, message = make(FakeHandler(OSError('reset')))
    message.retry_count = 2
    await engine._process_message(message)
    assert message.retry_count == 3
    assert message.status == MessageStatus.FAILED
    assert not engine._retries
    stored = engine.stored(message)
    assert (stored.status, stored.retry_count) == (MessageStatus.FAILED, 3)


@pytest.mark.asyncio
async def test_stop_cancels_pending_retry():
    engine, message = make(FakeHandler(OSError('reset')))
    engine.running = True  # stop() is a no-op on an engine that never started
    await engine._process_message(message)
    (task,) = engine._retries
    await engine.stop()
    assert task.cancelled()
    assert engine.message_queue.empty()

"""Unit tests for DeliveryEngine._process_message."""

import asyncio

import pytest
from smpp import CommandStatus
from smpp.exceptions import SMPPBindException, SMPPMessageException

from porth.config.settings import DeliveryConfig, Settings
from porth.core.delivery import DeliveryEngine, retry_delay
from porth.core.exceptions import MessageError
from porth.core.message import MessageStatus, SMSMessage
from porth.core.queue import MessageQueue
from porth.core.store import MessageStore


class FakeHandler:
    def __init__(self, error: Exception | None = None):
        self.error = error

    async def send_message(self, message):
        if self.error:
            raise self.error
        return {'smsc_message_ids': ['smsc-7', 'smsc-8']}


def make(handler=None, settings=None):
    engine = DeliveryEngine(
        MessageQueue(), MessageStore(), settings or Settings()
    )  # max_retries = 3
    engine.smpp_client = handler
    message = SMSMessage(
        source_addr='A',
        destination_addr='B',
        message_text='hi',
        protocol='http',
        status=MessageStatus.QUEUED,  # as taken off the queue
    )
    return engine, message


@pytest.mark.asyncio
async def test_success_marks_sent_with_smsc_id():
    engine, message = make(FakeHandler())
    await engine._process_message(message)
    assert message.status == MessageStatus.SENT
    assert message.sent_at is not None
    assert message.protocol_data['smsc_message_ids'] == ['smsc-7', 'smsc-8']
    assert engine.message_store.find_by_smsc_id('smsc-7') is message
    assert engine.message_store.find_by_smsc_id('smsc-8') is message


@pytest.mark.asyncio
async def test_message_error_fails_without_retry():
    engine, message = make(FakeHandler(MessageError('too long')))
    await engine._process_message(message)
    assert message.status == MessageStatus.FAILED
    assert message.retry_count == 0
    assert not engine._retries


@pytest.mark.asyncio
async def test_no_client_goes_through_retry():
    engine, message = make(None)
    await engine._process_message(message)
    assert message.retry_count == 1
    assert message.status == MessageStatus.QUEUED
    assert len(engine._retries) == 1


def test_retry_delay_backs_off_to_the_cap():
    config = DeliveryConfig(retry_delay=5, backoff_factor=2, max_retry_delay=30)
    assert [retry_delay(n, config) for n in range(1, 6)] == [5, 10, 20, 30, 30]
    config = DeliveryConfig(retry_delay=5, backoff_factor=1)
    assert [retry_delay(n, config) for n in range(1, 6)] == [5] * 5


@pytest.mark.asyncio
async def test_retries_wait_independently():
    engine, _ = make(settings=Settings(delivery=DeliveryConfig(retry_delay=1)))
    messages = [make()[1] for _ in range(5)]
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


@pytest.mark.asyncio
async def test_stop_cancels_pending_retry():
    engine, message = make(FakeHandler(OSError('reset')))
    engine.running = True  # stop() is a no-op on an engine that never started
    await engine._process_message(message)
    (task,) = engine._retries
    await engine.stop()
    assert task.cancelled()
    assert engine.message_queue.empty()

"""Unit tests for DeliveryEngine._process_message."""

import pytest

from porth.config.settings import Settings
from porth.core.delivery import DeliveryEngine
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
        return {'smsc_message_id': 'smsc-7'}


def make(handler=None):
    engine = DeliveryEngine(
        MessageQueue(), MessageStore(), Settings()
    )  # max_retries = 3
    engine.smpp_client = handler
    message = SMSMessage(
        source_addr='A', destination_addr='B', message_text='hi', protocol='http'
    )
    return engine, message


@pytest.mark.asyncio
async def test_success_marks_sent_with_smsc_id():
    engine, message = make(FakeHandler())
    await engine._process_message(message)
    assert message.status == MessageStatus.SENT
    assert message.sent_at is not None
    assert message.protocol_data['smsc_message_ids'] == ['smsc-7']
    assert engine.message_store.find_by_smsc_id('smsc-7') is message


@pytest.mark.asyncio
async def test_message_error_fails_without_retry():
    engine, message = make(FakeHandler(MessageError('too long')))
    await engine._process_message(message)
    assert message.status == MessageStatus.FAILED
    assert message.retry_count == 0
    assert engine.retry_queue.empty()


@pytest.mark.asyncio
async def test_no_client_goes_through_retry():
    engine, message = make(None)
    await engine._process_message(message)
    assert message.retry_count == 1
    assert message.status == MessageStatus.QUEUED
    assert engine.retry_queue.get_nowait() is message

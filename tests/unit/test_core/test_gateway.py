"""Unit tests for SMSGateway listener lifecycle."""

import asyncio
import socket
from datetime import datetime, timedelta, timezone

import pytest

from porth.config.settings import HTTPConfig, KannelConfig, Settings
from porth.core.message import MessageStatus, SMSMessage
from porth.main import SMSGateway


@pytest.mark.asyncio
async def test_failed_bind_leaves_runner_for_stop_to_clean_up(uow_factory):
    with socket.socket() as taken:
        taken.bind(('127.0.0.1', 0))
        taken.listen()
        port = taken.getsockname()[1]
        gateway = SMSGateway(
            Settings(
                http=HTTPConfig(host='127.0.0.1', port=0),
                kannel=KannelConfig(host='127.0.0.1', port=port),
            ),
            uow_factory,
        )
        with pytest.raises(OSError):
            await gateway.start()
        # the Kannel runner is tracked despite its failed bind
        assert len(gateway.servers) == 2
        await gateway.stop()
    assert all(runner.server is None for runner in gateway.servers)


def test_no_db_and_no_factory_raises():
    with pytest.raises(ValueError, match='db is not set'):
        SMSGateway(Settings())


def stored(repo, status, minutes_ago):
    message = SMSMessage(
        source_addr='A',
        destination_addr='B',
        message_text=status.value,
        protocol='http',
        status=status,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )
    repo.messages[message.message_id] = message
    return message


@pytest.mark.asyncio
async def test_start_requeues_unsent_oldest_first_and_resumes_callbacks(
    uow_factory, monkeypatch, caplog
):
    repo = uow_factory.repo
    newer = stored(repo, MessageStatus.QUEUED, 1)
    older = stored(repo, MessageStatus.PENDING, 5)
    for status in (MessageStatus.SENT, MessageStatus.DELIVERED, MessageStatus.FAILED):
        stored(repo, status, 3)
    done = stored(repo, MessageStatus.DELIVERED, 2)
    repo.dlr_callbacks[done.message_id] = 'http://127.0.0.1:1/dlr'

    gateway = SMSGateway(
        Settings(
            http=HTTPConfig(host='127.0.0.1', port=0),
            kannel=KannelConfig(host='127.0.0.1', port=0),
        ),
        uow_factory,
    )
    resumed = []
    monkeypatch.setattr(
        gateway.dlr_handler, '_fetch', lambda *args: _record(resumed, args)
    )
    # no workers: what start() queued stays on the queue to be read
    monkeypatch.setattr(gateway.delivery_engine, 'start', _noop)
    await gateway.start()
    try:
        queue = gateway.message_queue
        ids = [(await queue.get()).message_id for _ in range(queue.qsize())]
        assert ids == [older.message_id, newer.message_id]
        assert 'Re-queued 2 message(s) from the store' in caplog.text
        await asyncio.sleep(0)
        assert resumed == [(done.message_id, 'http://127.0.0.1:1/dlr')]
    finally:
        await gateway.stop()


async def _noop():
    pass


async def _record(into, args):
    into.append(args)

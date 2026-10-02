"""Unit tests for the Message Store sweep (receipt timeout and eviction)."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from porth.config.settings import StoreConfig
from porth.core.dlr import DLRHandler
from porth.core.message import MessageStatus, SMSMessage
from porth.core.sweep import Sweeper

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def stored(repo, status, age, dlr_requested=True, **fields):
    """A message created and (if sent) sent age ago; returns its id."""
    message = SMSMessage(
        source_addr='A',
        destination_addr='B',
        message_text='hi',
        protocol=fields.pop('protocol', 'http'),
        status=status,
        dlr_requested=dlr_requested,
        created_at=NOW - age,
        sent_at=NOW - age if status != MessageStatus.QUEUED else None,
        smsc='a',
        **fields,
    )
    repo.messages[message.message_id] = message
    return message.message_id


@pytest.fixture
def sweeper(uow_factory, monkeypatch):
    handler = DLRHandler(uow_factory)
    monkeypatch.setattr(handler, 'dispatch', lambda call: None)  # no HTTP here
    return Sweeper(uow_factory, handler, StoreConfig())


@pytest.mark.asyncio
async def test_expires_unreceipted_and_stores_rest_callback(sweeper, uow_factory):
    repo = uow_factory.repo
    rest = stored(
        repo, MessageStatus.SENT, timedelta(hours=49), callback_url='http://c/s'
    )
    kannel = stored(
        repo,
        MessageStatus.SENT,
        timedelta(hours=49),
        protocol='kannel',
        dlr_url='http://k/%d',
        protocol_data={'dlr_mask': 31},
    )
    fresh = stored(repo, MessageStatus.SENT, timedelta(hours=47))
    mo_reply = stored(
        repo, MessageStatus.SENT, timedelta(hours=49), dlr_requested=False
    )

    await sweeper.sweep_once(NOW)

    status = {id: m.status for id, m in repo.messages.items()}
    assert status[rest] == status[kannel] == MessageStatus.EXPIRED
    assert status[fresh] == status[mo_reply] == MessageStatus.SENT
    url, body = repo.dlr_callbacks[rest]
    assert (url, body['status']) == ('http://c/s', 'expired')
    assert kannel not in repo.dlr_callbacks  # a Kannel dlr-url is receipt-driven only


@pytest.mark.asyncio
async def test_evicts_finished_keeps_unfinished(sweeper, uow_factory):
    repo = uow_factory.repo
    old = timedelta(days=8)
    delivered = stored(repo, MessageStatus.DELIVERED, old)
    mo_reply = stored(repo, MessageStatus.SENT, old, dlr_requested=False)
    queued = stored(repo, MessageStatus.QUEUED, old)
    recent = stored(repo, MessageStatus.FAILED, timedelta(days=6))
    repo.smsc_ids[('a', 's1')] = delivered

    # Pinned before the sweep's own expiry pass would expire it
    awaiting = stored(repo, MessageStatus.SENT, timedelta(hours=1))
    repo.messages[awaiting].created_at = NOW - old

    await sweeper.sweep_once(NOW)

    assert set(repo.messages) == {queued, recent, awaiting}
    assert repo.smsc_ids == {}
    assert mo_reply not in repo.messages


@pytest.mark.asyncio
async def test_message_with_a_stored_call_is_kept_until_the_call_is_made(
    sweeper, uow_factory
):
    repo = uow_factory.repo
    # Created 8 days ago, times out now: expired and its call stored in this pass
    timed_out = stored(
        repo, MessageStatus.SENT, timedelta(hours=49), callback_url='http://c/s'
    )
    repo.messages[timed_out].created_at = NOW - timedelta(days=8)

    await sweeper.sweep_once(NOW)
    assert repo.messages[timed_out].status == MessageStatus.EXPIRED
    assert timed_out in repo.dlr_callbacks

    del repo.dlr_callbacks[timed_out]  # the call is made
    await sweeper.sweep_once(NOW)
    assert timed_out not in repo.messages


@pytest.mark.asyncio
async def test_stop_ends_the_task(sweeper):
    sweeper.start()
    task = sweeper._task
    await asyncio.sleep(0)  # first pass runs
    await sweeper.stop()
    assert task.done() and sweeper._task is None

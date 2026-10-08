"""The message repository against a real PostgreSQL (make db-up migrate first)."""

import os
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
import sqlalchemy as sa
from monobase.db import make_engine
from sqlalchemy.exc import IntegrityError

from porth.adapters.tables import dlr_callbacks, messages, smsc_ids
from porth.domain.model import MessageStatus, SMSMessage
from porth.service_layer.unit_of_work import SqlAlchemyUnitOfWork
from tests.integration.conftest import DSN

pytestmark = pytest.mark.skipif(
    not os.environ.get('RUN_INTEGRATION_TESTS'),
    reason='needs PostgreSQL: RUN_INTEGRATION_TESTS=1 (make test-integration)',
)


@pytest_asyncio.fixture
async def db(database):
    """(engine, ids): every message id put in ids is deleted afterwards."""
    engine = make_engine(DSN)
    ids: list[str] = []
    yield engine, ids
    async with engine.begin() as conn:
        await conn.execute(sa.delete(messages).where(messages.c.message_id.in_(ids)))
    await engine.dispose()


def new(ids, status=MessageStatus.QUEUED, **fields) -> SMSMessage:
    message = SMSMessage(
        source_addr='A',
        destination_addr='B',
        message_text='hi',
        protocol='kannel',
        status=status,
        **fields,
    )
    ids.append(message.message_id)
    return message


async def add(engine, *items: SMSMessage) -> None:
    async with SqlAlchemyUnitOfWork(engine) as uow:
        for item in items:
            await uow.messages.add(item)
        await uow.commit()


@pytest.mark.asyncio
async def test_add_get_round_trips_every_column(db):
    engine, ids = db
    now = datetime.now(timezone.utc).replace(microsecond=0)
    message = new(
        ids,
        status=MessageStatus.DELIVERED,
        correlation_id='c-1',
        protocol_data={'dlr_mask': 3, 'smsc_message_ids': ['s1'], 'x': {'y': None}},
        created_at=now - timedelta(minutes=1),
        sent_at=now - timedelta(seconds=30),
        delivered_at=now,
        retry_count=2,
        max_retries=5,
        dlr_requested=False,
        dlr_url='http://x/?d=%d',
        smsc='op-a',
        priority='high',
        valid_until=now + timedelta(minutes=5),
        keep_text=False,
        idempotency_key='k-1',
    )
    await add(engine, message)
    async with SqlAlchemyUnitOfWork(engine) as uow:
        got = await uow.messages.get(message.message_id)
        assert await uow.messages.get('not-a-uuid') is None
    assert got == message
    assert got.created_at.tzinfo is not None


@pytest.mark.asyncio
async def test_leaving_without_commit_leaves_no_row(db):
    engine, ids = db
    message = new(ids)
    async with SqlAlchemyUnitOfWork(engine) as uow:
        await uow.messages.add(message)
    async with SqlAlchemyUnitOfWork(engine) as uow:
        assert await uow.messages.get(message.message_id) is None


@pytest.mark.asyncio
async def test_update_writes_the_status_fields(db):
    engine, ids = db
    message = new(ids)
    await add(engine, message)
    message.status = MessageStatus.SENT
    message.retry_count = 1
    message.sent_at = datetime.now(timezone.utc)
    message.protocol_data['smsc_message_ids'] = ['s1']
    message.smsc = 'op-b'
    async with SqlAlchemyUnitOfWork(engine) as uow:
        await uow.messages.update(message)
        await uow.commit()
    async with SqlAlchemyUnitOfWork(engine) as uow:
        assert await uow.messages.get(message.message_id) == message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'keep_text, status, text',
    [
        (False, MessageStatus.SENT, ''),
        (False, MessageStatus.EXPIRED, ''),
        (False, MessageStatus.QUEUED, 'hi'),  # a retry still needs it
        (True, MessageStatus.SENT, 'hi'),
    ],
)
async def test_update_blanks_text_not_kept_once_sent_or_final(
    db, keep_text, status, text
):
    engine, ids = db
    message = new(ids, keep_text=keep_text)
    await add(engine, message)
    message.status = status
    async with SqlAlchemyUnitOfWork(engine) as uow:
        await uow.messages.update(message)
        await uow.commit()
    async with SqlAlchemyUnitOfWork(engine) as uow:
        assert (await uow.messages.get(message.message_id)).message_text == text
    assert message.message_text == 'hi'


@pytest.mark.asyncio
async def test_smsc_id_maps_to_the_latest_message(db):
    engine, ids = db
    first, second = new(ids), new(ids)
    smsc_id = f'smsc-{first.message_id}'
    await add(engine, first, second)
    for message in (first, second):
        async with SqlAlchemyUnitOfWork(engine) as uow:
            await uow.messages.add_smsc_ids(message.message_id, 'a', [smsc_id, 'other'])
            await uow.commit()
    async with SqlAlchemyUnitOfWork(engine) as uow:
        got = await uow.messages.get_by_smsc_id_for_update('a', smsc_id)
        assert await uow.messages.get_by_smsc_id_for_update('a', 'nope') is None
        assert await uow.messages.get_by_smsc_id_for_update('b', smsc_id) is None
    assert got is not None and got.message_id == second.message_id


@pytest.mark.asyncio
async def test_same_smsc_id_from_two_smscs_is_two_messages(db):
    engine, ids = db
    first, second = new(ids), new(ids)
    smsc_id = f'smsc-{first.message_id}'
    await add(engine, first, second)
    async with SqlAlchemyUnitOfWork(engine) as uow:
        await uow.messages.add_smsc_ids(first.message_id, 'a', [smsc_id])
        await uow.messages.add_smsc_ids(second.message_id, 'b', [smsc_id])
        await uow.commit()
    async with SqlAlchemyUnitOfWork(engine) as uow:
        got_a = await uow.messages.get_by_smsc_id_for_update('a', smsc_id)
        got_b = await uow.messages.get_by_smsc_id_for_update('b', smsc_id)
    assert got_a is not None and got_a.message_id == first.message_id
    assert got_b is not None and got_b.message_id == second.message_id


@pytest.mark.asyncio
async def test_unsent_is_pending_and_queued_oldest_first(db):
    engine, ids = db
    now = datetime.now(timezone.utc)
    queued = new(ids, created_at=now - timedelta(minutes=1))
    pending = new(ids, MessageStatus.PENDING, created_at=now - timedelta(minutes=2))
    others = [new(ids, status) for status in MessageStatus if status not in
              (MessageStatus.PENDING, MessageStatus.QUEUED)]  # fmt: skip
    await add(engine, queued, *others, pending)
    async with SqlAlchemyUnitOfWork(engine) as uow:
        unsent = [m.message_id for m in await uow.messages.unsent()]
    ours = [i for i in unsent if i in ids]  # the database may hold other rows
    assert ours == [pending.message_id, queued.message_id]


@pytest.mark.asyncio
async def test_callbacks_add_list_delete(db):
    engine, ids = db
    message = new(ids)
    await add(engine, message)
    async with SqlAlchemyUnitOfWork(engine) as uow:
        await uow.messages.add_callback(message.message_id, 'http://x/?d=1')
        await uow.commit()
    async with SqlAlchemyUnitOfWork(engine) as uow:
        assert (
            message.message_id,
            'http://x/?d=1',
            None,
        ) in await uow.messages.callbacks()
        await uow.messages.delete_callback(message.message_id)
        await uow.commit()
    async with SqlAlchemyUnitOfWork(engine) as uow:
        assert message.message_id not in [c[0] for c in await uow.messages.callbacks()]


@pytest.mark.asyncio
async def test_callback_body_and_callback_url_round_trip(db):
    engine, ids = db
    message = new(ids, callback_url='https://m/t/tok')
    await add(engine, message)
    body = {'message_id': message.message_id, 'status': 'failed', 'occurred_at': 'x'}
    async with SqlAlchemyUnitOfWork(engine) as uow:
        await uow.messages.add_callback(message.message_id, 'https://m/t/tok', body)
        await uow.commit()
    async with SqlAlchemyUnitOfWork(engine) as uow:
        assert (await uow.messages.get(message.message_id)).callback_url == (
            'https://m/t/tok'
        )
        assert (message.message_id, 'https://m/t/tok', body) in (
            await uow.messages.callbacks()
        )


@pytest.mark.asyncio
async def test_status_check_rejects_bogus(db):
    engine, ids = db
    message = new(ids)
    row = {c.name: getattr(message, c.name) for c in messages.columns}
    with pytest.raises(IntegrityError, match='messages_status_check'):
        async with engine.begin() as conn:
            await conn.execute(sa.insert(messages).values({**row, 'status': 'bogus'}))


@pytest.mark.asyncio
async def test_priority_defaults_to_normal_and_rejects_bogus(db):
    engine, ids = db
    message = new(ids)
    row = {c.name: getattr(message, c.name) for c in messages.columns}
    del row['priority']  # a row written before the column existed
    async with engine.begin() as conn:
        await conn.execute(sa.insert(messages).values(row))
    async with SqlAlchemyUnitOfWork(engine) as uow:
        assert (await uow.messages.get(message.message_id)).priority == 'normal'
    with pytest.raises(IntegrityError, match='messages_priority_check'):
        async with engine.begin() as conn:
            await conn.execute(
                sa.update(messages)
                .where(messages.c.message_id == message.message_id)
                .values(priority='urgent')
            )


@pytest.mark.asyncio
async def test_deleting_a_message_cascades(db):
    engine, ids = db
    message = new(ids)
    await add(engine, message)
    async with SqlAlchemyUnitOfWork(engine) as uow:
        await uow.messages.add_smsc_ids(
            message.message_id, 'a', [f's-{message.message_id}']
        )
        await uow.messages.add_callback(message.message_id, 'http://x/', {'s': 'x'})
        await uow.commit()
    async with engine.begin() as conn:
        await conn.execute(
            sa.delete(messages).where(messages.c.message_id == message.message_id)
        )
        for table in (smsc_ids, dlr_callbacks):
            count = await conn.scalar(
                sa.select(sa.func.count())
                .select_from(table)
                .where(table.c.message_id == message.message_id)
            )
            assert count == 0


# Far in the past, so the database's other rows stay out of the sweep queries
LONG_AGO = datetime(2000, 1, 1, tzinfo=timezone.utc)
CUTOFF = LONG_AGO + timedelta(days=1)


@pytest.mark.asyncio
async def test_unreceipted_is_sent_awaiting_a_receipt_before_cutoff(db):
    engine, ids = db
    older = new(ids, MessageStatus.SENT, sent_at=LONG_AGO)
    newer = new(ids, MessageStatus.SENT, sent_at=LONG_AGO + timedelta(hours=1))
    others = [
        new(ids, MessageStatus.SENT, sent_at=CUTOFF + timedelta(hours=1)),
        new(ids, MessageStatus.SENT, sent_at=LONG_AGO, dlr_requested=False),
        new(ids, MessageStatus.DELIVERED, sent_at=LONG_AGO),
    ]
    await add(engine, newer, older, *others)
    async with SqlAlchemyUnitOfWork(engine) as uow:
        got = [m.message_id for m in await uow.messages.unreceipted(CUTOFF, 10)]
        assert [m.message_id for m in await uow.messages.unreceipted(CUTOFF, 1)] == [
            older.message_id
        ]
    assert got == [older.message_id, newer.message_id]


@pytest.mark.asyncio
async def test_unreceipted_skips_a_row_another_transaction_holds(db):
    engine, ids = db
    held = new(ids, MessageStatus.SENT, sent_at=LONG_AGO)
    free = new(ids, MessageStatus.SENT, sent_at=LONG_AGO + timedelta(hours=1))
    await add(engine, held, free)
    async with engine.begin() as conn:
        await conn.execute(
            sa.select(messages)
            .where(messages.c.message_id == held.message_id)
            .with_for_update()
        )
        async with SqlAlchemyUnitOfWork(engine) as uow:
            got = [m.message_id for m in await uow.messages.unreceipted(CUTOFF, 10)]
    assert got == [free.message_id]


@pytest.mark.asyncio
async def test_evict_deletes_finished_before_cutoff_with_their_smsc_ids(db):
    engine, ids = db
    doomed = [
        new(ids, status, created_at=LONG_AGO)
        for status in (
            MessageStatus.DELIVERED,
            MessageStatus.FAILED,
            MessageStatus.EXPIRED,
        )
    ]
    doomed.append(
        new(ids, MessageStatus.SENT, created_at=LONG_AGO, dlr_requested=False)
    )
    kept = [
        new(ids, MessageStatus.PENDING, created_at=LONG_AGO),
        new(ids, MessageStatus.QUEUED, created_at=LONG_AGO),
        new(ids, MessageStatus.SENT, created_at=LONG_AGO),  # awaiting its receipt
        new(ids, MessageStatus.DELIVERED, created_at=CUTOFF + timedelta(hours=1)),
        new(ids, MessageStatus.EXPIRED, created_at=LONG_AGO),  # its call still owed
    ]
    await add(engine, *doomed, *kept)
    first, owed = doomed[0].message_id, kept[-1].message_id
    async with SqlAlchemyUnitOfWork(engine) as uow:
        await uow.messages.add_smsc_ids(first, 'a', [f's-{first}'])
        await uow.messages.add_callback(owed, 'http://x/', {'s': 'x'})
        await uow.commit()

    async with SqlAlchemyUnitOfWork(engine) as uow:
        assert await uow.messages.evict(CUTOFF, 3) == 3
        assert await uow.messages.evict(CUTOFF, 10) == 1
        await uow.commit()

    async with engine.begin() as conn:
        left = await conn.scalars(
            sa.select(messages.c.message_id).where(messages.c.message_id.in_(ids))
        )
        assert set(left) == {m.message_id for m in kept}
        count = await conn.scalar(
            sa.select(sa.func.count())
            .select_from(smsc_ids)
            .where(smsc_ids.c.message_id == first)
        )
        assert count == 0

    # Once its call is made, the owed message goes too
    async with SqlAlchemyUnitOfWork(engine) as uow:
        await uow.messages.delete_callback(owed)
        assert await uow.messages.evict(CUTOFF, 10) == 1
        await uow.commit()


@pytest.mark.asyncio
async def test_idempotency_key_is_unique_and_found(db):
    engine, ids = db
    first = new(ids, idempotency_key='k-2')
    await add(engine, first, new(ids), new(ids))  # no key: NULLs never collide
    with pytest.raises(IntegrityError, match='messages_idempotency_key_key'):
        await add(engine, new(ids, idempotency_key='k-2'))
    async with SqlAlchemyUnitOfWork(engine) as uow:
        assert await uow.messages.get_by_idempotency_key('k-2') == first
        assert await uow.messages.get_by_idempotency_key('k-none') is None

"""The message repository against a real PostgreSQL (make db-up migrate first)."""

import os
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
import sqlalchemy as sa
from monobase.db import make_engine
from sqlalchemy.exc import IntegrityError

from porth.adapters.tables import dlr_callbacks, messages, smsc_ids
from porth.core.message import MessageStatus, SMSMessage
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
    async with SqlAlchemyUnitOfWork(engine) as uow:
        await uow.messages.update(message)
        await uow.commit()
    async with SqlAlchemyUnitOfWork(engine) as uow:
        assert await uow.messages.get(message.message_id) == message


@pytest.mark.asyncio
async def test_smsc_id_maps_to_the_latest_message(db):
    engine, ids = db
    first, second = new(ids), new(ids)
    smsc_id = f'smsc-{first.message_id}'
    await add(engine, first, second)
    for message in (first, second):
        async with SqlAlchemyUnitOfWork(engine) as uow:
            await uow.messages.add_smsc_ids(message.message_id, [smsc_id, 'other'])
            await uow.commit()
    async with SqlAlchemyUnitOfWork(engine) as uow:
        got = await uow.messages.get_by_smsc_id_for_update(smsc_id)
        assert await uow.messages.get_by_smsc_id_for_update('nope') is None
    assert got is not None and got.message_id == second.message_id


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
        assert (message.message_id, 'http://x/?d=1') in await uow.messages.callbacks()
        await uow.messages.delete_callback(message.message_id)
        await uow.commit()
    async with SqlAlchemyUnitOfWork(engine) as uow:
        assert message.message_id not in dict(await uow.messages.callbacks())


@pytest.mark.asyncio
async def test_status_check_rejects_bogus(db):
    engine, ids = db
    message = new(ids)
    row = {c.name: getattr(message, c.name) for c in messages.columns}
    with pytest.raises(IntegrityError, match='messages_status_check'):
        async with engine.begin() as conn:
            await conn.execute(sa.insert(messages).values({**row, 'status': 'bogus'}))


@pytest.mark.asyncio
async def test_deleting_a_message_cascades(db):
    engine, ids = db
    message = new(ids)
    await add(engine, message)
    async with SqlAlchemyUnitOfWork(engine) as uow:
        await uow.messages.add_smsc_ids(message.message_id, [f's-{message.message_id}'])
        await uow.messages.add_callback(message.message_id, 'http://x/')
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

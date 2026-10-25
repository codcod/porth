"""The message store against a real database: the sweep's expiry and retention on
PostgreSQL and SQLite, the Alembic revisions on PostgreSQL only (SQLite's schema comes
from ensure_schema, so it has nothing to migrate)."""

import asyncio
from datetime import timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from aiohttp import web
from aiohttp.test_utils import TestServer
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import create_async_engine

from porth.adapters.db import make_engine
from porth.adapters.tables import dlr_callbacks, metadata, smsc_ids
from porth.config.settings import StoreConfig
from porth.domain.model import MessageStatus
from porth.service_layer.dlr import DLRHandler
from porth.service_layer.sweep import Sweeper
from porth.service_layer.unit_of_work import SqlAlchemyUnitOfWork
from tests.integration.conftest import DSN, SQLITE
from tests.integration.test_repository import (  # noqa: F401
    LONG_AGO,
    add,
    db,
    new,
    pytestmark,
)
from tests.integration.test_restart import until

postgres_only = pytest.mark.skipif(
    SQLITE, reason='SQLite has no migrations: ensure_schema builds its schema'
)


class Target:
    """Records REST status callback POSTs and Kannel dlr-url GETs."""

    def __init__(self) -> None:
        self.posts: list[dict] = []
        self.gets: list[dict[str, str]] = []
        app = web.Application()
        app.router.add_post('/rest', self.post)
        app.router.add_get('/dlr', self.get)
        self.server = TestServer(app)

    async def post(self, request: web.Request) -> web.Response:
        self.posts.append(await request.json())
        return web.Response()

    async def get(self, request: web.Request) -> web.Response:
        self.gets.append(dict(request.query))
        return web.Response()

    def url(self, path: str) -> str:
        return str(self.server.make_url(path))


# The sweep passes at this time, so its cutoffs fall before every other row in the
# shared database: only a test's own rows, aged from it, are expired or deleted
SWEPT_AT = LONG_AGO


@pytest_asyncio.fixture
async def sweeper(db):  # noqa: F811 (the imported fixture)
    """(sweeper, engine, ids) over a started DLRHandler."""
    engine, ids = db
    handler = DLRHandler(lambda: SqlAlchemyUnitOfWork(engine))
    await handler.start()
    yield (
        Sweeper(lambda: SqlAlchemyUnitOfWork(engine), handler, StoreConfig()),
        engine,
        ids,
    )
    await handler.stop()


async def status(engine, message_id: str) -> MessageStatus | None:
    async with SqlAlchemyUnitOfWork(engine) as uow:
        message = await uow.messages.get(message_id)
    return message and message.status


async def count(engine, table: sa.Table, message_id: str) -> int:
    async with engine.connect() as conn:
        return await conn.scalar(
            sa.select(sa.func.count())
            .select_from(table)
            .where(table.c.message_id == message_id)
        )


@pytest.mark.asyncio
async def test_sweep_expires_unreceipted_messages_and_calls_their_callback_url(
    sweeper,
):
    sweep, engine, ids = sweeper
    now = SWEPT_AT
    target = Target()
    await target.server.start_server()
    try:
        rest = new(ids, MessageStatus.SENT, sent_at=now - timedelta(hours=49))
        rest.protocol = 'http'
        rest.callback_url = target.url('/rest')
        # Due on a failure receipt (bit 2), but expiry is not receipt-driven
        kannel = new(
            ids,
            MessageStatus.SENT,
            sent_at=now - timedelta(hours=49),
            dlr_url=target.url('/dlr') + '?d=%d',
            protocol_data={'dlr_mask': 3},
        )
        fresh = new(ids, MessageStatus.SENT, sent_at=now - timedelta(hours=47))
        no_dlr = new(
            ids,
            MessageStatus.SENT,
            sent_at=now - timedelta(hours=49),
            dlr_requested=False,
        )
        await add(engine, rest, kannel, fresh, no_dlr)

        await sweep.sweep_once(now)

        assert await status(engine, rest.message_id) is MessageStatus.EXPIRED
        assert await status(engine, kannel.message_id) is MessageStatus.EXPIRED
        assert await status(engine, fresh.message_id) is MessageStatus.SENT
        assert await status(engine, no_dlr.message_id) is MessageStatus.SENT
        await until(lambda: target.posts)
        await until(
            lambda: _no_callback(engine, rest.message_id)
        )  # forgotten once made
        assert [(p['message_id'], p['status']) for p in target.posts] == [
            (rest.message_id, 'expired')
        ]
        assert target.gets == []
        assert await count(engine, dlr_callbacks, kannel.message_id) == 0
    finally:
        await target.server.close()


async def _no_callback(engine, message_id: str) -> bool:
    return await count(engine, dlr_callbacks, message_id) == 0


@pytest.mark.asyncio
async def test_sweep_deletes_finished_messages_past_retention_with_their_rows(
    sweeper,
):
    sweep, engine, ids = sweeper
    now = SWEPT_AT
    old = new(ids, MessageStatus.DELIVERED, created_at=now - timedelta(days=8))
    owed = new(ids, MessageStatus.DELIVERED, created_at=now - timedelta(days=8))
    recent = new(ids, MessageStatus.DELIVERED, created_at=now - timedelta(days=6))
    await add(engine, old, owed, recent)
    async with SqlAlchemyUnitOfWork(engine) as uow:
        await uow.messages.add_smsc_ids(old.message_id, 'smsc-a', [old.message_id])
        # A dead URL: only the stored row matters, the sweep never makes this call
        await uow.messages.add_callback(owed.message_id, 'http://127.0.0.1:9/')
        await uow.commit()

    await sweep.sweep_once(now)

    assert await status(engine, old.message_id) is None
    assert await count(engine, smsc_ids, old.message_id) == 0
    assert await status(engine, owed.message_id) is MessageStatus.DELIVERED
    assert await status(engine, recent.message_id) is MessageStatus.DELIVERED


# Migrations: sync tests, as env.py runs its own asyncio.run


@pytest.fixture
def migrations_db() -> str:
    """A fresh porth_migrations_test database's DSN: a downgrade to base would drop
    the shared porth_test's tables under every other test."""
    url = sa.make_url(DSN).set(database='porth_migrations_test')

    async def recreate() -> None:
        engine = create_async_engine(
            url.set(database='postgres'), isolation_level='AUTOCOMMIT'
        )
        try:
            async with engine.connect() as conn:
                await conn.execute(
                    sa.text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)')
                )
                await conn.execute(sa.text(f'CREATE DATABASE "{url.database}"'))
        finally:
            await engine.dispose()

    asyncio.run(recreate())
    return url.render_as_string(hide_password=False)


def alembic_config(dsn: str) -> Config:
    config = Config()
    config.set_main_option('script_location', 'porth:migrations')
    config.attributes['dsn'] = dsn
    return config


def alembic(dsn: str, cmd: str, rev: str) -> None:
    getattr(command, cmd)(alembic_config(dsn), rev)


def sql(dsn: str, statement: str, **params) -> list[sa.Row]:
    async def run() -> list[sa.Row]:
        engine = create_async_engine(dsn)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(sa.text(statement), params)
                return list(result) if result.returns_rows else []
        finally:
            await engine.dispose()

    return asyncio.run(run())


def porth_tables(dsn: str) -> set[str]:
    rows = sql(
        dsn,
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'porth'",
    )
    return {row.table_name for row in rows}


@postgres_only
def test_migrations_upgrade_downgrade_and_upgrade_again(migrations_db):
    alembic(migrations_db, 'upgrade', 'head')
    alembic(migrations_db, 'downgrade', 'base')
    assert porth_tables(migrations_db) <= {'alembic_version'}

    alembic(migrations_db, 'upgrade', 'head')
    assert porth_tables(migrations_db) == {
        'alembic_version',
        'messages',
        'smsc_ids',
        'dlr_callbacks',
    }
    head = ScriptDirectory.from_config(alembic_config(migrations_db)).get_current_head()
    assert sql(migrations_db, 'SELECT version_num FROM porth.alembic_version') == [
        (head,)
    ]


@postgres_only
def test_route_by_smsc_keeps_messages_and_empties_smsc_ids(migrations_db):
    alembic(migrations_db, 'upgrade', 'd8f0999f7682')
    sql(
        migrations_db,
        """INSERT INTO porth.messages (message_id, source_addr, destination_addr,
            message_text, status, protocol, protocol_data, created_at, retry_count,
            max_retries, dlr_requested)
        VALUES ('m1', 'A', 'B', 'hi', 'sent', 'kannel', '{}', now(), 0, 3, true)""",
    )
    sql(
        migrations_db,
        "INSERT INTO porth.smsc_ids (smsc_id, message_id) VALUES ('s1', 'm1')",
    )

    alembic(migrations_db, 'upgrade', 'ee72044ce98d')
    assert sql(migrations_db, 'SELECT smsc FROM porth.messages') == [(None,)]
    assert sql(migrations_db, 'SELECT * FROM porth.smsc_ids') == []

    alembic(migrations_db, 'upgrade', 'head')

    async def get():
        engine = make_engine(migrations_db)
        try:
            async with SqlAlchemyUnitOfWork(engine) as uow:
                return await uow.messages.get('m1')
        finally:
            await engine.dispose()

    message = asyncio.run(get())
    assert message is not None
    assert message.status is MessageStatus.SENT
    assert message.smsc is None
    assert message.priority == 'normal'
    assert message.keep_text is True


@postgres_only
def test_models_match_the_migrations(migrations_db):
    alembic(migrations_db, 'upgrade', 'head')

    def include_name(name, type_, parent_names) -> bool:
        if type_ == 'schema':
            return name == 'porth'
        return not (type_ == 'table' and name == 'alembic_version')

    def compare(conn) -> list:
        context = MigrationContext.configure(
            conn,
            opts={
                'include_schemas': True,
                'compare_server_default': True,
                'version_table_schema': 'porth',
                'include_name': include_name,
                'target_metadata': metadata,
            },
        )
        return compare_metadata(context, metadata)

    async def diff() -> list:
        # The porth role's search_path ("$user") makes porth the default schema, so
        # alembic would reflect its tables without a schema and see all of them as new
        engine = create_async_engine(
            migrations_db, connect_args={'server_settings': {'search_path': 'public'}}
        )
        try:
            async with engine.connect() as conn:
                return await conn.run_sync(compare)
        finally:
            await engine.dispose()

    assert asyncio.run(diff()) == [], (
        'porth.adapters.tables and the migrations disagree: write a revision '
        '(and a SQLite schema step, see test_db.py)'
    )

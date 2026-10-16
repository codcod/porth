"""porth's engine, on PostgreSQL or a SQLite file, and the SQLite schema."""

import typing as tp

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from porth.adapters.tables import metadata

# The SQLite schema's PRAGMA user_version. The Alembic revisions are PostgreSQL-only:
# a new revision needs a SQLite upgrade step in ensure_schema and a bump here.
SQLITE_SCHEMA_VERSION = 1
# How long a transaction waits for SQLite's write lock before it fails
BUSY_TIMEOUT_MS = 10000


def sqlite_path(dsn: str) -> tp.Optional[str]:
    """The database file of a SQLite DSN ('' or ':memory:' for an in-memory one);
    None for any other backend."""
    url = sa.make_url(dsn)
    return url.database or '' if url.get_backend_name() == 'sqlite' else None


def make_engine(dsn: str) -> AsyncEngine:
    if sqlite_path(dsn) is None:
        # hide_parameters: an error's text never carries a message's number or text
        return create_async_engine(dsn, pool_pre_ping=True, hide_parameters=True)
    # No schemas on SQLite: porth's tables live in the main database
    engine = create_async_engine(
        dsn,
        hide_parameters=True,
        execution_options={'schema_translate_map': {'porth': None}},
    )

    @event.listens_for(engine.sync_engine, 'connect')
    def _connect(dbapi_connection: tp.Any, _record: tp.Any) -> None:
        # SQLAlchemy, not the driver, begins transactions (_begin below)
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        # synchronous stays FULL: NORMAL may lose the last commits on power loss
        for pragma in (
            'journal_mode = WAL',
            f'busy_timeout = {BUSY_TIMEOUT_MS}',
            'foreign_keys = ON',
        ):
            cursor.execute(f'PRAGMA {pragma}')
        cursor.close()

    @event.listens_for(engine.sync_engine, 'begin')
    def _begin(conn: sa.Connection) -> None:
        # The write lock up front, so a read-then-write never fails with
        # SQLITE_BUSY_SNAPSHOT; a busy database is waited out (busy_timeout)
        # ponytail: every transaction serialised (one writer); a read-only UoW could
        # use a deferred BEGIN if throughput matters
        conn.exec_driver_sql('BEGIN IMMEDIATE')

    return engine


async def ensure_schema(engine: AsyncEngine) -> None:
    """On SQLite, create the schema in a new database file, and refuse one of another
    version; a no-op on PostgreSQL, whose schema Alembic migrates."""
    if engine.dialect.name != 'sqlite':
        return
    async with engine.begin() as conn:
        version = await conn.scalar(sa.text('PRAGMA user_version'))
        if version == SQLITE_SCHEMA_VERSION:
            return
        if version != 0:
            raise RuntimeError(
                f'SQLite schema version {version}, this porth expects '
                f'{SQLITE_SCHEMA_VERSION}: the database was written by another porth '
                f'version'
            )
        await conn.run_sync(metadata.create_all)
        await conn.execute(sa.text(f'PRAGMA user_version = {SQLITE_SCHEMA_VERSION}'))

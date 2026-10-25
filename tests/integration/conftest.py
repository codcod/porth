"""The integration tests' own database, created and migrated once per run: PostgreSQL,
or a SQLite file when TEST_PORTH_DB names one (make test-integration-sqlite)."""

import asyncio
import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine

from porth.adapters.db import ensure_schema, make_engine, sqlite_path

# Not the dev database: test_restart's gateway sends every unsent row it finds.
DSN = os.environ.get(
    'TEST_PORTH_DB', 'postgresql+asyncpg://porth:porth@localhost:5432/porth_test'
)
SQLITE = sqlite_path(DSN) is not None


@pytest.fixture(scope='session')
def database() -> str:
    """Create DSN's database if missing and migrate it to head."""
    if SQLITE:  # porth's own schema step, as at gateway start
        asyncio.run(_ensure_sqlite())
        return DSN
    asyncio.run(_create_database(sa.make_url(DSN)))
    config = Config()
    config.set_main_option('script_location', 'porth:migrations')
    config.attributes['dsn'] = DSN
    command.upgrade(config, 'head')
    return DSN


async def _ensure_sqlite() -> None:
    engine = make_engine(DSN)
    try:
        await ensure_schema(engine)
    finally:
        await engine.dispose()


async def _create_database(url: sa.URL) -> None:
    engine = create_async_engine(
        url.set(database='postgres'), isolation_level='AUTOCOMMIT'
    )
    try:
        async with engine.connect() as conn:
            exists = await conn.scalar(
                sa.text('SELECT 1 FROM pg_database WHERE datname = :name'),
                {'name': url.database},
            )
            if not exists:
                await conn.execute(sa.text(f'CREATE DATABASE "{url.database}"'))
    finally:
        await engine.dispose()

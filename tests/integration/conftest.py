"""The PostgreSQL tests' own database, created and migrated once per run."""

import asyncio
import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine

# Not the dev database: test_restart's gateway sends every unsent row it finds.
DSN = os.environ.get(
    'TEST_PORTH_DB', 'postgresql+asyncpg://porth:porth@localhost:5432/porth_test'
)


@pytest.fixture(scope='session')
def database() -> str:
    """Create DSN's database if missing and migrate it to head."""
    asyncio.run(_create_database(sa.make_url(DSN)))
    config = Config()
    config.set_main_option(
        'script_location', str(Path(__file__).parents[2] / 'migrations')
    )
    config.attributes['dsn'] = DSN
    command.upgrade(config, 'head')
    return DSN


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

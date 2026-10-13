"""porth's SQLite schema step, on a throwaway file (no server)."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.exc import OperationalError

from porth.adapters import db
from porth.adapters.db import SQLITE_SCHEMA_VERSION, ensure_schema, make_engine
from porth.config.settings import Settings
from porth.entrypoints.app import Gateway
from porth.service_layer.unit_of_work import SqlAlchemyUnitOfWork


@pytest.mark.asyncio
async def test_ensure_schema_creates_a_new_file_once_and_refuses_another_version(
    tmp_path,
):
    engine = make_engine(f'sqlite+aiosqlite:///{tmp_path}/porth.db')
    try:
        await ensure_schema(engine)
        await ensure_schema(engine)  # the same version: nothing to do
        async with engine.begin() as conn:
            tables = await conn.scalars(
                sa.text("SELECT name FROM sqlite_master WHERE type = 'table'")
            )
            assert set(tables) == {'messages', 'smsc_ids', 'dlr_callbacks'}
            assert await conn.scalar(sa.text('PRAGMA user_version')) == 1
            await conn.execute(sa.text('PRAGMA user_version = 7'))
        with pytest.raises(RuntimeError, match='SQLite schema version 7'):
            await ensure_schema(engine)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_unit_of_work_that_cannot_begin_gives_its_connection_back(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(db, 'BUSY_TIMEOUT_MS', 50)
    engine = make_engine(f'sqlite+aiosqlite:///{tmp_path}/porth.db')
    try:
        await ensure_schema(engine)
        async with engine.connect() as holder:
            await holder.begin()  # holds the write lock
            with pytest.raises(OperationalError, match='database is locked'):
                async with SqlAlchemyUnitOfWork(engine):
                    pass
        assert engine.pool.checkedout() == 0
    finally:
        await engine.dispose()


def test_a_new_alembic_revision_needs_a_sqlite_schema_step():
    config = Config()
    config.set_main_option(
        'script_location', str(Path(__file__).parents[2] / 'migrations')
    )
    assert ScriptDirectory.from_config(config).get_heads() == ['33126464e69d'], (
        'a new revision needs a SQLite upgrade step in porth.adapters.db and a '
        'SQLITE_SCHEMA_VERSION bump'
    )
    assert SQLITE_SCHEMA_VERSION == 1


@pytest.mark.parametrize('db', ['sqlite+aiosqlite:///:memory:', 'sqlite+aiosqlite://'])
def test_an_in_memory_sqlite_database_is_refused(db):
    with pytest.raises(ValueError, match='in-memory SQLite'):
        Gateway(Settings(db=db))

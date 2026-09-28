from logging.config import fileConfig

from alembic import context
from monobase.migrations import run_migrations_online

from porth.adapters.tables import metadata
from porth.config.settings import load_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata


def get_dsn() -> str:
    """The DSN from porth's own settings (the same loader the gateway uses)."""
    dsn = load_settings().db
    if not dsn:
        raise SystemExit(
            'db is not set: set PORTH_DB, or db: in the PORTH_CONFIG_FILE YAML'
        )
    return dsn


if context.is_offline_mode():
    context.configure(
        url=get_dsn(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={'paramstyle': 'named'},
        version_table_schema=target_metadata.schema,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    run_migrations_online(target_metadata, get_dsn())

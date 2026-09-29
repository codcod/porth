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
    """
    The DSN from porth's own settings (the same loader the gateway uses; the file is
    config/config.toml unless `-x config=<path>` names another), unless the caller
    passed one in config.attributes['dsn'] (the integration tests do).
    """
    path = context.get_x_argument(as_dictionary=True).get(
        'config', 'config/config.toml'
    )
    dsn = config.attributes.get('dsn') or load_settings(path).db
    if not dsn:
        raise SystemExit(f'db is not set: set db in the [porth] table of {path}')
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
        # Before Alembic's version table, which lives in the schema (online mode
        # gets this from run_migrations_online)
        context.execute(f'CREATE SCHEMA IF NOT EXISTS {target_metadata.schema}')
        context.run_migrations()
else:
    run_migrations_online(target_metadata, get_dsn())

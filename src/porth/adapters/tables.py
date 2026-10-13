"""porth's tables (PostgreSQL schema porth, or the main database on SQLite), declared
once for the app and Alembic."""

import typing as tp
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


class UTCDateTime(sa.TypeDecorator[datetime]):
    """A timestamptz that reads back aware: SQLite has no time zones, and stores
    porth's UTC values naive."""

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_result_value(
        self, value: tp.Optional[datetime], dialect: sa.Dialect
    ) -> tp.Optional[datetime]:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


JSON = sa.JSON().with_variant(JSONB(), 'postgresql')

metadata = sa.MetaData(schema='porth')

messages = sa.Table(
    'messages',
    metadata,
    sa.Column('message_id', sa.Text(), primary_key=True),
    sa.Column('correlation_id', sa.Text(), nullable=True),
    sa.Column('source_addr', sa.Text(), nullable=False),
    sa.Column('destination_addr', sa.Text(), nullable=False),
    sa.Column('message_text', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('protocol', sa.Text(), nullable=False),
    sa.Column('protocol_data', JSON, nullable=False),
    sa.Column('created_at', UTCDateTime(), nullable=False),
    sa.Column('sent_at', UTCDateTime(), nullable=True),
    sa.Column('delivered_at', UTCDateTime(), nullable=True),
    sa.Column('retry_count', sa.Integer(), nullable=False),
    sa.Column('max_retries', sa.Integer(), nullable=False),
    sa.Column('dlr_requested', sa.Boolean(), nullable=False),
    sa.Column('dlr_url', sa.Text(), nullable=True),
    sa.Column('smsc', sa.Text(), nullable=True),  # null: stored before routing
    sa.Column('priority', sa.Text(), nullable=False, server_default='normal'),
    sa.Column('callback_url', sa.Text(), nullable=True),
    sa.Column('valid_until', UTCDateTime(), nullable=True),
    sa.Column('keep_text', sa.Boolean(), nullable=False, server_default=sa.true()),
    sa.Column('idempotency_key', sa.Text(), nullable=True),
    sa.CheckConstraint(
        "status IN ('pending','queued','sent','delivered','failed','expired')",
        name='messages_status_check',
    ),
    sa.CheckConstraint("protocol IN ('http','kannel')", name='messages_protocol_check'),
    sa.CheckConstraint("priority IN ('normal','high')", name='messages_priority_check'),
    # NULLs never collide: a message without a key is unconstrained
    sa.UniqueConstraint('idempotency_key', name='messages_idempotency_key_key'),
)

# The receipt lookup index: (SMSC, its message id) -> the latest message that got it
smsc_ids = sa.Table(
    'smsc_ids',
    metadata,
    sa.Column('smsc', sa.Text(), primary_key=True),
    sa.Column('smsc_id', sa.Text(), primary_key=True),
    sa.Column(
        'message_id',
        sa.Text(),
        sa.ForeignKey(messages.c.message_id, ondelete='CASCADE'),
        nullable=False,
    ),
)

# A final-status call not yet made, at most one per message: a Kannel dlr-url GET
# (expanded URL, null body) or a REST status callback POST of body
dlr_callbacks = sa.Table(
    'dlr_callbacks',
    metadata,
    sa.Column(
        'message_id',
        sa.Text(),
        sa.ForeignKey(messages.c.message_id, ondelete='CASCADE'),
        primary_key=True,
    ),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('body', JSON, nullable=True),
)

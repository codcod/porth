"""porth's PostgreSQL tables (schema porth), declared once for the app and Alembic."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

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
    sa.Column('protocol_data', JSONB(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('retry_count', sa.Integer(), nullable=False),
    sa.Column('max_retries', sa.Integer(), nullable=False),
    sa.Column('dlr_requested', sa.Boolean(), nullable=False),
    sa.Column('dlr_url', sa.Text(), nullable=True),
    sa.Column('smsc', sa.Text(), nullable=True),  # null: stored before routing
    sa.CheckConstraint(
        "status IN ('pending','queued','sent','delivered','failed','expired')",
        name='messages_status_check',
    ),
    sa.CheckConstraint("protocol IN ('http','kannel')", name='messages_protocol_check'),
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

# A dlr-url call not yet made (expanded URL); at most one per message
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
)

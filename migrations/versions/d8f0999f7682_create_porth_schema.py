"""create porth schema

Revision ID: d8f0999f7682
Revises:
Create Date: 2026-09-28 08:57:29.926715

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd8f0999f7682'
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    _ = op.create_table(
        'messages',
        sa.Column('message_id', sa.Text(), nullable=False),
        sa.Column('correlation_id', sa.Text(), nullable=True),
        sa.Column('source_addr', sa.Text(), nullable=False),
        sa.Column('destination_addr', sa.Text(), nullable=False),
        sa.Column('message_text', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('protocol', sa.Text(), nullable=False),
        sa.Column('protocol_data', postgresql.JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('retry_count', sa.Integer(), nullable=False),
        sa.Column('max_retries', sa.Integer(), nullable=False),
        sa.Column('dlr_requested', sa.Boolean(), nullable=False),
        sa.Column('dlr_url', sa.Text(), nullable=True),
        sa.CheckConstraint(
            "protocol IN ('http','kannel')", name='messages_protocol_check'
        ),
        sa.CheckConstraint(
            "status IN ('pending','queued','sent','delivered','failed','expired')",
            name='messages_status_check',
        ),
        sa.PrimaryKeyConstraint('message_id'),
        schema='porth',
    )
    _ = op.create_table(
        'smsc_ids',
        sa.Column('smsc_id', sa.Text(), nullable=False),
        sa.Column('message_id', sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ['message_id'], ['porth.messages.message_id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('smsc_id'),
        schema='porth',
    )
    _ = op.create_table(
        'dlr_callbacks',
        sa.Column('message_id', sa.Text(), nullable=False),
        sa.Column('url', sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ['message_id'], ['porth.messages.message_id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('message_id'),
        schema='porth',
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Not dropping the porth schema itself: alembic's own version table
    # (version_table_schema='porth', see monobase.migrations) lives in it and
    # alembic still needs it to record this downgrade.
    op.drop_table('dlr_callbacks', schema='porth')
    op.drop_table('smsc_ids', schema='porth')
    op.drop_table('messages', schema='porth')

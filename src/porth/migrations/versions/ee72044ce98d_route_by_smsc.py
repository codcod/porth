"""route by smsc: messages.smsc, smsc_ids keyed by (smsc, smsc_id) (POR-020)

Existing smsc_ids rows are deleted, not backfilled: only the config knows the one
SMSC they came from, and persistence was never released, so they are development
rows. Receipts for them go unmatched.

Revision ID: ee72044ce98d
Revises: d8f0999f7682
Create Date: 2026-09-29 18:05:47.396067

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'ee72044ce98d'
down_revision: str | Sequence[str] | None = 'd8f0999f7682'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('messages', sa.Column('smsc', sa.Text()), schema='porth')
    op.execute('DELETE FROM porth.smsc_ids')
    op.add_column(
        'smsc_ids', sa.Column('smsc', sa.Text(), nullable=False), schema='porth'
    )
    op.drop_constraint('smsc_ids_pkey', 'smsc_ids', schema='porth')
    op.create_primary_key(
        'smsc_ids_pkey', 'smsc_ids', ['smsc', 'smsc_id'], schema='porth'
    )


def downgrade() -> None:
    """Downgrade schema."""
    # The same smsc_id may now be indexed for two SMSCs, so the rows cannot keep it
    op.execute('DELETE FROM porth.smsc_ids')
    op.drop_constraint('smsc_ids_pkey', 'smsc_ids', schema='porth')
    op.create_primary_key('smsc_ids_pkey', 'smsc_ids', ['smsc_id'], schema='porth')
    op.drop_column('smsc_ids', 'smsc', schema='porth')
    op.drop_column('messages', 'smsc', schema='porth')

"""validity and keep_text: messages.valid_until, messages.keep_text (POR-027)

Every existing row keeps its text (keep_text true) and has no validity.

Revision ID: 888cb4b538b4
Revises: 262f43ee6358
Create Date: 2026-10-02 22:36:26.072936

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '888cb4b538b4'
down_revision: str | Sequence[str] | None = '262f43ee6358'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'messages',
        sa.Column('valid_until', sa.DateTime(timezone=True), nullable=True),
        schema='porth',
    )
    op.add_column(
        'messages',
        sa.Column('keep_text', sa.Boolean(), server_default=sa.true(), nullable=False),
        schema='porth',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('messages', 'keep_text', schema='porth')
    op.drop_column('messages', 'valid_until', schema='porth')

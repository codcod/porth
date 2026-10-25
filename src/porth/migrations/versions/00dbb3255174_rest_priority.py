"""rest priority: messages.priority, high or normal (POR-025)

Existing rows take the server default, normal.

Revision ID: 00dbb3255174
Revises: ee72044ce98d
Create Date: 2026-10-02 21:33:16.256202

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '00dbb3255174'
down_revision: str | Sequence[str] | None = 'ee72044ce98d'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'messages',
        sa.Column('priority', sa.Text(), nullable=False, server_default='normal'),
        schema='porth',
    )
    # Autogenerate misses check constraints
    op.create_check_constraint(
        'messages_priority_check',
        'messages',
        "priority IN ('normal','high')",
        schema='porth',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('messages_priority_check', 'messages', schema='porth')
    op.drop_column('messages', 'priority', schema='porth')

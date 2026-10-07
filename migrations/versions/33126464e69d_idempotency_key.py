"""idempotency key: messages.idempotency_key, unique (POR-028)

Every existing row has no key; NULLs never collide.

Revision ID: 33126464e69d
Revises: 888cb4b538b4
Create Date: 2026-10-07 13:53:05.482061

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '33126464e69d'
down_revision: str | Sequence[str] | None = '888cb4b538b4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'messages',
        sa.Column('idempotency_key', sa.Text(), nullable=True),
        schema='porth',
    )
    op.create_unique_constraint(
        'messages_idempotency_key_key', 'messages', ['idempotency_key'], schema='porth'
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        'messages_idempotency_key_key', 'messages', schema='porth', type_='unique'
    )
    op.drop_column('messages', 'idempotency_key', schema='porth')

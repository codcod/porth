"""rest status callback: messages.callback_url, dlr_callbacks.body (POR-026)

A null body is a Kannel dlr-url GET (every existing row), a body a REST POST of it.

Revision ID: 262f43ee6358
Revises: 00dbb3255174
Create Date: 2026-10-02 21:46:44.953664

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '262f43ee6358'
down_revision: str | Sequence[str] | None = '00dbb3255174'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'messages', sa.Column('callback_url', sa.Text(), nullable=True), schema='porth'
    )
    op.add_column(
        'dlr_callbacks',
        sa.Column('body', postgresql.JSONB(), nullable=True),
        schema='porth',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('dlr_callbacks', 'body', schema='porth')
    op.drop_column('messages', 'callback_url', schema='porth')

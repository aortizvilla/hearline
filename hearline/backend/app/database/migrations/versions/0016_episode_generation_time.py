"""episode generation time

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-22 20:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0016'
down_revision: str | Sequence[str] | None = '0015'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('fact_episode', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ready_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('generation_seconds', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('timing', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('fact_episode', schema=None) as batch_op:
        batch_op.drop_column('timing')
        batch_op.drop_column('generation_seconds')
        batch_op.drop_column('ready_at')

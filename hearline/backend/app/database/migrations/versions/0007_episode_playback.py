"""episode playback: progress_seconds, started_at, last_played_at

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-22 17:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: str | Sequence[str] | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('fact_episode', schema=None) as batch_op:
        batch_op.add_column(sa.Column('progress_seconds', sa.Integer(), nullable=False,
                                      server_default='0'))
        batch_op.add_column(sa.Column('started_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('last_played_at', sa.DateTime(), nullable=True))
        batch_op.create_index(batch_op.f('ix_fact_episode_last_played_at'), ['last_played_at'],
                              unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('fact_episode', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_fact_episode_last_played_at'))
        batch_op.drop_column('last_played_at')
        batch_op.drop_column('started_at')
        batch_op.drop_column('progress_seconds')

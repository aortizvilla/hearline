"""episode accuracy review

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-22 19:20:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401  (autogenerate renders sqlmodel string types)
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0014'
down_revision: str | Sequence[str] | None = '0013'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'dim_episode_review',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('key', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('language', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('issues', sa.JSON(), nullable=True),
        sa.Column('model', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('dim_episode_review', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_dim_episode_review_key'), ['key'], unique=True)

    with op.batch_alter_table('fact_episode', schema=None) as batch_op:
        batch_op.add_column(sa.Column('quality', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('fact_episode', schema=None) as batch_op:
        batch_op.drop_column('quality')

    with op.batch_alter_table('dim_episode_review', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_dim_episode_review_key'))

    op.drop_table('dim_episode_review')

"""user plan and subscriptions

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-22 17:30:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401  (autogenerate renders sqlmodel string types)
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0012'
down_revision: str | Sequence[str] | None = '0011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('dim_user', schema=None) as batch_op:
        # everyone who signed up before plans existed starts on Free
        batch_op.add_column(sa.Column('plan', sqlmodel.sql.sqltypes.AutoString(), nullable=False,
                                      server_default='free'))
        batch_op.create_index(batch_op.f('ix_dim_user_plan'), ['plan'], unique=False)

    op.create_table(
        'fact_subscription',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('plan', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('price_usd_month', sa.Float(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('ended_at', sa.DateTime(), nullable=True),
        sa.Column('source', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('provider', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('provider_ref', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['dim_user.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('fact_subscription', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_fact_subscription_plan'), ['plan'], unique=False)
        batch_op.create_index('ix_fact_subscription_user_status', ['user_id', 'status'],
                              unique=False)

    # existing users: an active Free subscription from their sign-up date
    op.execute(
        "INSERT INTO fact_subscription (user_id, plan, status, price_usd_month, started_at, "
        "source, provider, provider_ref) "
        "SELECT id, 'free', 'active', 0, created_at, 'migration', '', '' FROM dim_user"
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('fact_subscription', schema=None) as batch_op:
        batch_op.drop_index('ix_fact_subscription_user_status')
        batch_op.drop_index(batch_op.f('ix_fact_subscription_plan'))
    op.drop_table('fact_subscription')
    with op.batch_alter_table('dim_user', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_dim_user_plan'))
        batch_op.drop_column('plan')

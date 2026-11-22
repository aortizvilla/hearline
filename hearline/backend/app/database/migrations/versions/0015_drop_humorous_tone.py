"""drop the humorous tone

It was offered in Settings but played as conversational. Profiles that chose it are moved to
conversational, which is what they were already hearing.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-22 19:40:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0015'
down_revision: str | Sequence[str] | None = '0014'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("UPDATE dim_profile SET tone = 'conversational' WHERE tone = 'humorous'")


def downgrade() -> None:
    """Downgrade schema (data stays on conversational: it can't tell who had chosen humorous)."""

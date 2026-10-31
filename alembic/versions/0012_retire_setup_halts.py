"""Retire the setup system's loss halts (README §5.9, #34): the daily and weekly loss halts
belonged to an earlier design; the portfolio has only the kill switch and the drawdown halt,
and nothing reads the two rows any more.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0012'
down_revision: str | Sequence[str] | None = '0011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RETIRED = ("halt_daily_loss", "halt_weekly_loss")


def upgrade() -> None:
    """Upgrade schema."""
    flags = sa.table("system_flags", sa.column("name", sa.Text))
    op.execute(flags.delete().where(flags.c.name.in_(RETIRED)))


def downgrade() -> None:
    """Downgrade schema."""
    flags = sa.table("system_flags", sa.column("name", sa.Text), sa.column("value", sa.Boolean))
    op.bulk_insert(flags, [{"name": name, "value": False} for name in RETIRED])

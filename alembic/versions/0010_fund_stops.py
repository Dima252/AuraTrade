"""Rebalance orders (task 5.1, README §5.6, §7.2): fund_stops (the leveraged fund's stop level,
tighten-only) and each rebalance's runs on portfolio_targets (executions, executed_at) for the
retry.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-27

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0010'
down_revision: str | Sequence[str] | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('fund_stops',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('symbol', sa.Text(), nullable=False),
    sa.Column('session_date', sa.Date(), nullable=False),
    sa.Column('level', sa.Double(), nullable=False),
    sa.Column('change', sa.Text(), nullable=False),
    sa.Column('decision_price', sa.Double(), nullable=True),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("change IN ('ENTRY', 'RAISE', 'SPLIT')", name=op.f('ck_fund_stops_change')),
    sa.CheckConstraint('level > 0', name=op.f('ck_fund_stops_level')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_fund_stops'))
    )
    op.create_index('ix_fund_stops_symbol', 'fund_stops', ['symbol', 'id'], unique=False)
    op.add_column('portfolio_targets', sa.Column('executions', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'"), nullable=False))
    op.add_column('portfolio_targets', sa.Column('executed_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('portfolio_targets', 'executed_at')
    op.drop_column('portfolio_targets', 'executions')
    op.drop_index('ix_fund_stops_symbol', table_name='fund_stops')
    op.drop_table('fund_stops')

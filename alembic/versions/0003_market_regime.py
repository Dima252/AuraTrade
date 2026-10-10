"""market_regime: the computed regime per session (task 2.3, README §5.2 / §10).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REGIMES = "('CORRECTION', 'EARLY_UPTREND', 'UNDER_PRESSURE', 'CONFIRMED_UPTREND')"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "market_regime",
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("index_trend", sa.SmallInteger(), nullable=False),
        sa.Column("breadth", sa.SmallInteger(), nullable=False),
        sa.Column("volatility", sa.SmallInteger(), nullable=False),
        sa.Column("distribution", sa.SmallInteger(), nullable=False),
        sa.Column("leadership", sa.SmallInteger(), nullable=False),
        sa.Column("score", sa.SmallInteger(), nullable=False),
        sa.Column("computed_regime", sa.Text(), nullable=False),
        sa.Column("llm_regime", sa.Text(), nullable=True),
        sa.Column("effective_regime", sa.Text(), nullable=False),
        sa.Column("crisis", sa.Boolean(), nullable=False),
        sa.Column("follow_through_day", sa.Boolean(), nullable=False),
        sa.Column("no_ftd", sa.Boolean(), nullable=False),
        sa.Column("sessions_in_state", sa.SmallInteger(), nullable=False),
        sa.Column("expiration_session", sa.Boolean(), nullable=False),
        sa.Column(
            "features",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("risk_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"computed_regime IN {REGIMES}", name=op.f("ck_market_regime_computed_regime")
        ),
        sa.CheckConstraint(
            f"effective_regime IN {REGIMES}", name=op.f("ck_market_regime_effective_regime")
        ),
        sa.CheckConstraint(
            f"llm_regime IS NULL OR llm_regime IN {REGIMES}",
            name=op.f("ck_market_regime_llm_regime"),
        ),
        sa.CheckConstraint(
            "index_trend BETWEEN -1 AND 1 AND breadth BETWEEN -1 AND 1 "
            "AND volatility BETWEEN -1 AND 1 AND distribution BETWEEN -1 AND 1 "
            "AND leadership BETWEEN -1 AND 1",
            name=op.f("ck_market_regime_pillars"),
        ),
        sa.CheckConstraint(
            "score = index_trend + breadth + volatility + distribution + leadership",
            name=op.f("ck_market_regime_score"),
        ),
        sa.CheckConstraint(
            "sessions_in_state >= 1", name=op.f("ck_market_regime_sessions_in_state")
        ),
        sa.CheckConstraint(
            "NOT no_ftd OR computed_regime = 'EARLY_UPTREND'", name=op.f("ck_market_regime_no_ftd")
        ),
        sa.PrimaryKeyConstraint("session_date", name=op.f("pk_market_regime")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("market_regime")

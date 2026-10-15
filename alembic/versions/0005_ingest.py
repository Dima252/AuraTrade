"""Ingest (task 1.5, README §5.1).

bar_corrections (the outlier filter's audit trail); earnings timing provenance, fiscal period
and headline time; industry group descriptions; Finnhub's industry as the SIC fallback.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-15

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "bar_corrections",
        sa.Column("ticker_id", sa.Integer(), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("original", sa.REAL(), nullable=False),
        sa.Column("corrected", sa.REAL(), nullable=False),
        sa.Column(
            "detected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("field IN ('HIGH', 'LOW')", name=op.f("ck_bar_corrections_field")),
        sa.ForeignKeyConstraint(
            ["ticker_id"], ["tickers.id"], name=op.f("fk_bar_corrections_ticker_id_tickers")
        ),
        sa.PrimaryKeyConstraint(
            "ticker_id", "session_date", "field", name=op.f("pk_bar_corrections")
        ),
    )
    op.add_column(
        "earnings_events",
        sa.Column("timing_inferred", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("earnings_events", sa.Column("fiscal_year", sa.SmallInteger(), nullable=True))
    op.add_column("earnings_events", sa.Column("fiscal_quarter", sa.SmallInteger(), nullable=True))
    op.add_column(
        "earnings_events",
        sa.Column("headline_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("industry_groups", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("tickers", sa.Column("finnhub_industry", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("tickers", "finnhub_industry")
    op.drop_column("industry_groups", "description")
    op.drop_column("earnings_events", "headline_at")
    op.drop_column("earnings_events", "fiscal_quarter")
    op.drop_column("earnings_events", "fiscal_year")
    op.drop_column("earnings_events", "timing_inferred")
    op.drop_table("bar_corrections")

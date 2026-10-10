"""bases: detected bases and IBD base count (task 2.2, README §5.3 / §10).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "bases",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("ticker_id", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("sessions", sa.SmallInteger(), nullable=False),
        sa.Column("high", sa.Double(), nullable=False),
        sa.Column("low", sa.Double(), nullable=False),
        sa.Column("low_date", sa.Date(), nullable=False),
        sa.Column("depth", sa.Double(), nullable=False),
        sa.Column("pivot", sa.Double(), nullable=False),
        sa.Column("contraction_count", sa.SmallInteger(), nullable=False),
        sa.Column(
            "contractions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column("stage_number", sa.SmallInteger(), nullable=True),
        sa.Column("count_reliable", sa.Boolean(), nullable=False),
        sa.Column("advance", sa.Double(), nullable=True),
        sa.Column("reset", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('FORMING', 'BROKEN_OUT', 'FAILED')", name=op.f("ck_bases_status")
        ),
        sa.CheckConstraint(
            "(end_date IS NULL) = (status = 'FORMING')", name=op.f("ck_bases_end_date")
        ),
        sa.CheckConstraint(
            "(stage_number IS NULL) = (status = 'FAILED')", name=op.f("ck_bases_stage_number")
        ),
        sa.CheckConstraint(
            "stage_number IS NULL OR stage_number >= 1", name=op.f("ck_bases_stage_positive")
        ),
        sa.CheckConstraint(
            "end_date IS NULL OR end_date > start_date", name=op.f("ck_bases_ends_after_start")
        ),
        sa.CheckConstraint("low > 0 AND high >= low AND pivot > 0", name=op.f("ck_bases_prices")),
        sa.CheckConstraint("depth >= 0 AND depth < 1", name=op.f("ck_bases_depth")),
        sa.CheckConstraint("contraction_count >= 0", name=op.f("ck_bases_contraction_count")),
        sa.ForeignKeyConstraint(
            ["ticker_id"], ["tickers.id"], name=op.f("fk_bases_ticker_id_tickers")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bases")),
        sa.UniqueConstraint("ticker_id", "start_date", name=op.f("uq_bases_ticker_id_start_date")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("bases")

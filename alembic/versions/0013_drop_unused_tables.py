"""Drop the tables and columns nothing reads or writes (README §10): the stock-selection
tables, the regime's two pillars without inputs, the order layer's links to them, and the
weekly job's status. Buys are recorded as BUY.

Irreversible: the dropped rows are gone, so the downgrade refuses. After upgrading a database
with regime history, replay it: `python -m auratrade.jobs.regime --from <first> --session <last>`.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-31

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0013'
down_revision: str | Sequence[str] | None = '0012'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "verdict_outcomes",
    "baseline_signals",
    "breakout_tracker",
    "daily_watchlist",
    "trade_journal",
    "position_events",
    "position_plans",
    "positions",
    "signals",
    "bases",
    "earnings_events",
    "fundamentals",
    "industry_groups",
)
TICKER_COLUMNS = (
    "cusip",
    "cik",
    "exchange",
    "security_type",
    "sic",
    "finnhub_industry",
    "industry_group_id",
    "listed_at",
    "delisted_at",
)
JOB_STATUSES = (
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "SKIPPED_MARKET_CLOSED",
    "SKIPPED_ALREADY_RAN",
    "WAITING_PREDECESSOR",
    "BLOCKED_PREDECESSOR",
)
ORDER_PURPOSES = ("BUY", "STOP_LEG", "STOP", "EXIT", "OTHER")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    """Upgrade schema."""
    # CASCADE also drops the foreign keys into them (tickers.industry_group_id, orders').
    for table in TABLES:
        op.execute(f"DROP TABLE {table} CASCADE")
    for column in TICKER_COLUMNS:
        op.drop_column("tickers", column)

    op.drop_index("ix_orders_position", table_name="orders")
    op.drop_column("orders", "position_id")
    op.drop_column("orders", "signal_id")
    op.drop_constraint(op.f("ck_orders_purpose"), "orders", type_="check")
    op.execute("UPDATE orders SET purpose = 'BUY' WHERE purpose = 'ENTRY'")
    op.create_check_constraint(op.f("ck_orders_purpose"), "orders", _in("purpose", ORDER_PURPOSES))

    op.drop_column("account_snapshots", "risk_multiplier")

    op.drop_constraint(op.f("ck_job_runs_status"), "job_runs", type_="check")
    op.execute("DELETE FROM job_runs WHERE status = 'SKIPPED_NOT_SATURDAY'")
    op.create_check_constraint(op.f("ck_job_runs_status"), "job_runs", _in("status", JOB_STATUSES))

    for name in ("pillars", "score", "effective_regime", "llm_regime"):
        op.drop_constraint(op.f(f"ck_market_regime_{name}"), "market_regime", type_="check")
    for column in ("breadth", "leadership", "llm_regime", "effective_regime"):
        op.drop_column("market_regime", column)
    # Rows from before the two pillars lost their inputs scored them; a regime replay
    # (`jobs.regime --from`) relabels them on the three.
    op.execute("UPDATE market_regime SET score = index_trend + volatility + distribution")
    op.create_check_constraint(
        op.f("ck_market_regime_pillars"),
        "market_regime",
        "index_trend BETWEEN -1 AND 1 AND volatility BETWEEN -1 AND 1 "
        "AND distribution BETWEEN -1 AND 1",
    )
    op.create_check_constraint(
        op.f("ck_market_regime_score"),
        "market_regime",
        "score = index_trend + volatility + distribution",
    )


def downgrade() -> None:
    """Downgrade schema."""
    raise NotImplementedError("0013 drops tables and their rows; it can't be undone")

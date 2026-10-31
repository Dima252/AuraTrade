"""The data-layer tables (README §10). Every change here needs an Alembic migration.

Neon's free tier is 0.5 GB, so `daily_bars` — by far the largest table — uses 4-byte
floats for prices and orders its columns to avoid alignment padding.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    REAL,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Double,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint names, so migrations can refer to them.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

DATA_STATUSES = ("PENDING", "COMPLETE", "INCOMPLETE")
CALENDAR_CHECKS = ("UNCHECKED", "AGREE", "DISAGREE")
CORPORATE_ACTION_TYPES = (
    "SPLIT",
    "REVERSE_SPLIT",
    "CASH_MERGER",
    "STOCK_MERGER",
    "SPIN_OFF",
    "SYMBOL_CHANGE",
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


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy's documented class-level mapping
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
    }


def _created() -> Mapped[datetime]:
    return mapped_column(server_default=func.now())


class Ticker(Base):
    """A security. Tickers get reused, so the symbol alone isn't a key (#111)."""

    __tablename__ = "tickers"
    __table_args__ = (
        # One active security per symbol; reused symbols of delisted securities stay.
        Index("uq_tickers_active_symbol", "symbol", unique=True, postgresql_where=text("active")),
        Index("ix_tickers_symbol", "symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    symbol: Mapped[str] = mapped_column(Text)
    alpaca_asset_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), unique=True)
    name: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class MarketSession(Base):
    __tablename__ = "market_sessions"
    __table_args__ = (
        CheckConstraint(_in("data_status", DATA_STATUSES), name="data_status"),
        CheckConstraint(_in("calendar_check", CALENDAR_CHECKS), name="calendar_check"),
        CheckConstraint("close_at > open_at", name="open_before_close"),
        CheckConstraint("minutes > 0 AND minutes <= 390", name="minutes"),
        CheckConstraint("coverage IS NULL OR coverage BETWEEN 0 AND 1", name="coverage"),
    )

    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    open_at: Mapped[datetime]
    close_at: Mapped[datetime]
    is_early_close: Mapped[bool] = mapped_column(Boolean)
    minutes: Mapped[int] = mapped_column(SmallInteger)
    data_status: Mapped[str] = mapped_column(Text, server_default=text("'PENDING'"))
    coverage: Mapped[float | None] = mapped_column(REAL)
    data_checked_at: Mapped[datetime | None]
    # Alpaca is primary; pandas_market_calendars is the cross-check (README §4).
    calendar_check: Mapped[str] = mapped_column(Text, server_default=text("'UNCHECKED'"))
    calendar_check_detail: Mapped[str | None] = mapped_column(Text)
    synced_at: Mapped[datetime] = _created()


class DailyBar(Base):
    """Split-adjusted daily bars plus the raw close (point-in-time price filters)."""

    __tablename__ = "daily_bars"
    __table_args__ = (
        CheckConstraint(
            "open > 0 AND high > 0 AND low > 0 AND close > 0 AND raw_close > 0", name="positive"
        ),
        CheckConstraint(
            "high >= low AND high >= open AND high >= close AND low <= open AND low <= close",
            name="ohlc",
        ),
        CheckConstraint("volume > 0", name="volume"),
        # Bars arrive in date order, so a BRIN index is kilobytes where a btree would be
        # tens of megabytes of the 0.5 GB tier. Per-ticker reads use the primary key.
        Index("ix_daily_bars_session_date", "session_date", postgresql_using="brin"),
    )

    ticker_id: Mapped[int] = mapped_column(Integer, ForeignKey("tickers.id"), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    # Zero-volume bars are dropped at ingest (#111); volume can exceed int4 after split
    # adjustment.
    volume: Mapped[int] = mapped_column(BigInteger)
    open: Mapped[float] = mapped_column(REAL)
    high: Mapped[float] = mapped_column(REAL)
    low: Mapped[float] = mapped_column(REAL)
    close: Mapped[float] = mapped_column(REAL)
    raw_close: Mapped[float] = mapped_column(REAL)


BAR_FIELDS = ("HIGH", "LOW")


class BarCorrection(Base):
    """A bad high or low caught by ingest's outlier filter (README §5.1, #126). `daily_bars`
    holds the corrected value; the vendor's is kept here."""

    __tablename__ = "bar_corrections"
    __table_args__ = (CheckConstraint(_in("field", BAR_FIELDS), name="field"),)

    ticker_id: Mapped[int] = mapped_column(Integer, ForeignKey("tickers.id"), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    field: Mapped[str] = mapped_column(Text, primary_key=True)
    original: Mapped[float] = mapped_column(REAL)
    corrected: Mapped[float] = mapped_column(REAL)
    detected_at: Mapped[datetime] = _created()


class CorporateAction(Base):
    __tablename__ = "corporate_actions"
    __table_args__ = (
        CheckConstraint(_in("action_type", CORPORATE_ACTION_TYPES), name="action_type"),
        CheckConstraint("ratio IS NULL OR ratio > 0", name="ratio"),
        CheckConstraint("(handled_at IS NULL) = (NOT handled)", name="handled_at"),
        UniqueConstraint("source", "source_id"),
        Index("ix_corporate_actions_open", "ex_date", postgresql_where=text("NOT handled")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ticker_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("tickers.id"))
    symbol: Mapped[str] = mapped_column(Text)
    action_type: Mapped[str] = mapped_column(Text)
    ex_date: Mapped[date] = mapped_column(Date)
    # New shares per old share for splits (2.0 = 2-for-1, 0.1 = 1-for-10 reverse).
    ratio: Mapped[float | None] = mapped_column(Double)
    terms: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'"))
    source: Mapped[str] = mapped_column(Text)
    source_id: Mapped[str] = mapped_column(Text)
    detected_at: Mapped[datetime] = _created()
    handled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    handled_at: Mapped[datetime | None]


class MacroObservation(Base):
    """One value per series and date; `source` records which feed supplied it."""

    __tablename__ = "macro_series"

    series: Mapped[str] = mapped_column(Text, primary_key=True)  # VIX, VIX3M, CREDIT_SPREAD
    observation_date: Mapped[date] = mapped_column(Date, primary_key=True)
    value: Mapped[float] = mapped_column(Double)
    source: Mapped[str] = mapped_column(Text)  # CBOE, FRED
    fetched_at: Mapped[datetime] = _created()


class JobRunRow(Base):
    __tablename__ = "job_runs"
    __table_args__ = (
        CheckConstraint(_in("status", JOB_STATUSES), name="status"),
        CheckConstraint("finished_at IS NULL OR finished_at >= started_at", name="finished"),
        Index("ix_job_runs_job_session", "job", "session_date", "started_at"),
    )

    run_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    job: Mapped[str] = mapped_column(Text)
    session_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    trading_mode: Mapped[str] = mapped_column(Text)
    risk_hash: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'"))
    error: Mapped[str | None] = mapped_column(Text)


class SystemFlag(Base):
    """The kill switch (`trading_enabled`) and the halts. Read by every job at start;
    a missing row counts as the restrictive value."""

    __tablename__ = "system_flags"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[bool] = mapped_column(Boolean)
    details: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'"))
    updated_at: Mapped[datetime] = _created()
    updated_by: Mapped[str | None] = mapped_column(Text)


class Mismatch(Base):
    """An open row blocks entries in its symbol, or all entries when `symbol` is NULL."""

    __tablename__ = "mismatches"
    __table_args__ = (
        CheckConstraint(
            "(resolved_at IS NULL) = (resolution_note IS NULL)", name="resolution_note"
        ),
        Index("ix_mismatches_open", "symbol", postgresql_where=text("resolved_at IS NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(Text)  # RECONCILIATION, CORPORATE_ACTION, ...
    symbol: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'"))
    detected_at: Mapped[datetime] = _created()
    resolved_at: Mapped[datetime | None]
    resolution_note: Mapped[str | None] = mapped_column(Text)


REGIMES = ("CORRECTION", "EARLY_UPTREND", "UNDER_PRESSURE", "CONFIRMED_UPTREND")


class MarketRegimeRow(Base):
    """One row per session (README §5.2, task 2.3), written at the close; it governs the next
    session. `state` carries the state machine to the next evening. Kept forever (small)."""

    __tablename__ = "market_regime"
    __table_args__ = (
        CheckConstraint(_in("computed_regime", REGIMES), name="computed_regime"),
        CheckConstraint(
            "index_trend BETWEEN -1 AND 1 AND volatility BETWEEN -1 AND 1 "
            "AND distribution BETWEEN -1 AND 1",
            name="pillars",
        ),
        CheckConstraint(
            "score = index_trend + volatility + distribution",
            name="score",
        ),
        CheckConstraint("sessions_in_state >= 1", name="sessions_in_state"),
        CheckConstraint("NOT no_ftd OR computed_regime = 'EARLY_UPTREND'", name="no_ftd"),
    )

    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    index_trend: Mapped[int] = mapped_column(SmallInteger)
    volatility: Mapped[int] = mapped_column(SmallInteger)
    distribution: Mapped[int] = mapped_column(SmallInteger)
    score: Mapped[int] = mapped_column(SmallInteger)
    computed_regime: Mapped[str] = mapped_column(Text)
    crisis: Mapped[bool] = mapped_column(Boolean)
    follow_through_day: Mapped[bool] = mapped_column(Boolean)
    no_ftd: Mapped[bool] = mapped_column(Boolean)
    sessions_in_state: Mapped[int] = mapped_column(SmallInteger)
    expiration_session: Mapped[bool] = mapped_column(Boolean)
    features: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'"))
    state: Mapped[dict[str, Any]]
    risk_hash: Mapped[str] = mapped_column(String(64))
    computed_at: Mapped[datetime] = _created()


# --- The order layer (README §5.6, §10) -------------------------------------------------------

ORDER_PURPOSES = ("BUY", "STOP_LEG", "STOP", "EXIT", "OTHER")


class OrderRow(Base):
    """The broker's orders as last seen (README §10). Legs carry their parent's id."""

    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(_in("purpose", ORDER_PURPOSES), name="purpose"),
        Index("ix_orders_symbol_created", "symbol", "created_at"),
    )

    broker_id: Mapped[str] = mapped_column(Text, primary_key=True)
    client_order_id: Mapped[str] = mapped_column(Text)
    parent_broker_id: Mapped[str | None] = mapped_column(Text)
    replaces: Mapped[str | None] = mapped_column(Text)
    symbol: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text)
    side: Mapped[str] = mapped_column(Text)
    order_type: Mapped[str] = mapped_column(Text)
    order_class: Mapped[str] = mapped_column(Text)
    time_in_force: Mapped[str] = mapped_column(Text)
    qty: Mapped[int] = mapped_column(Integer)
    filled_qty: Mapped[int] = mapped_column(Integer)
    filled_avg_price: Mapped[float | None] = mapped_column(Double)
    limit_price: Mapped[float | None] = mapped_column(Double)
    stop_price: Mapped[float | None] = mapped_column(Double)
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime | None]
    filled_at: Mapped[datetime | None]
    synced_at: Mapped[datetime] = _created()


class AccountSnapshot(Base):
    """The account at each session's `post_market` (README §5.7)."""

    __tablename__ = "account_snapshots"

    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    equity: Mapped[float] = mapped_column(Double)
    cash: Mapped[float] = mapped_column(Double)
    long_market_value: Mapped[float] = mapped_column(Double)
    peak_equity: Mapped[float] = mapped_column(Double)
    drawdown: Mapped[float] = mapped_column(Double)
    open_positions: Mapped[int] = mapped_column(SmallInteger)
    details: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'"))
    holdings: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'"))
    """{symbol: {"units": .., "value": ..}} at the close, the funds only (task 5.3)."""
    tax_owed: Mapped[float | None] = mapped_column(Double)
    """The estimated tax if everything were sold at the close, from the live lots: equity less
    it is "after tax". None when the lots couldn't be rebuilt that evening."""
    created_at: Mapped[datetime] = _created()


# --- The portfolio manager (task 4.3, README §5.4-§5.7, §10) ---------------------------------

TARGET_KINDS = ("REBALANCE", "EVENT")
TARGET_STATUSES = ("ALLOCATED", "DECIDED")
BOOKS = ("TWIN", "SIM", "SPY")
LOT_KINDS = ("LOT", "INCOME")
STOP_CHANGES = ("ENTRY", "RAISE", "SPLIT")


class PromptVersion(Base):
    """A prompt as sent: its id is the version and the rendered text's hash (README §9)."""

    __tablename__ = "prompt_versions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class LlmDecision(Base):
    """One LLM decision: the answer or the failure, who answered, what it cost (README §9)."""

    __tablename__ = "llm_decisions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date)
    role: Mapped[str] = mapped_column(Text)
    prompt_id: Mapped[str] = mapped_column(Text, ForeignKey("prompt_versions.id"))
    requested_model: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    """The model that gave the last answer (a server-side fallback's, if it ran)."""
    effort: Mapped[str] = mapped_column(Text)
    failure: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text, server_default=text("''"))
    refusal_category: Mapped[str | None] = mapped_column(Text)
    answer: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    turns: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'"))
    cost_usd: Mapped[float] = mapped_column(Double)
    cost_alert: Mapped[bool] = mapped_column(Boolean)
    fallback: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = _created()


class PortfolioTarget(Base):
    """One decision: on R `allocate` writes the rule level and the facts (ALLOCATED); on E (or
    an event) `portfolio_manager` writes the enforced targets (DECIDED)."""

    __tablename__ = "portfolio_targets"
    __table_args__ = (
        CheckConstraint(_in("kind", TARGET_KINDS), name="kind"),
        CheckConstraint(_in("status", TARGET_STATUSES), name="status"),
        CheckConstraint(_in("trend", ("UP", "DOWN")), name="trend"),
        # One rebalance per E; events are rare and may repeat.
        Index(
            "uq_portfolio_targets_rebalance",
            "execution_session",
            unique=True,
            postgresql_where=text("kind = 'REBALANCE'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    ranking_session: Mapped[date] = mapped_column(Date)
    execution_session: Mapped[date] = mapped_column(Date)
    """E, or the event's session."""
    status: Mapped[str] = mapped_column(Text)
    trend: Mapped[str] = mapped_column(Text)
    capped: Mapped[bool] = mapped_column(Boolean)
    halted: Mapped[bool] = mapped_column(Boolean)
    rule_level: Mapped[float] = mapped_column(Double)
    twin_targets: Mapped[dict[str, Any]] = mapped_column(JSONB)
    facts: Mapped[dict[str, Any]] = mapped_column(JSONB)
    """The Portfolio Manager's facts as `allocate` gathered them (headlines are added on E)."""
    llm_decision_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("llm_decisions.id"))
    level: Mapped[float | None] = mapped_column(Double)
    core_level: Mapped[float | None] = mapped_column(Double)
    satellite: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    targets: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    findings: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'"))
    held_satellite: Mapped[bool | None] = mapped_column(Boolean)
    fell_back: Mapped[bool | None] = mapped_column(Boolean)
    satellite_tax: Mapped[float | None] = mapped_column(Double)
    """The tax the satellite's sales were estimated to realize (#163's budget counts it)."""
    risk_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()
    decided_at: Mapped[datetime | None]
    executions: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'"))
    """`execute_close`'s runs on a rebalance's targets (task 5.1): one entry per run."""
    executed_at: Mapped[datetime | None]
    """When a run finished every trade; until then the retry tries again (README §3)."""


class TaxLot(Base):
    """The live account's tax lots and income (README §5.7), rebuilt whole each evening from
    the broker's activities (`tax.live`, task 5.3): open lots, closed pieces, income."""

    __tablename__ = "tax_lots"
    __table_args__ = (CheckConstraint(_in("kind", LOT_KINDS), name="kind"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    symbol: Mapped[str] = mapped_column(Text)
    opened: Mapped[date] = mapped_column(Date)
    units: Mapped[float] = mapped_column(Double)
    cost: Mapped[float] = mapped_column(Double)
    """USD; for an INCOME row, the amount received."""
    closed: Mapped[date | None] = mapped_column(Date)
    proceeds: Mapped[float | None] = mapped_column(Double)
    realized_gain: Mapped[float | None] = mapped_column(Double)
    order_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class FundStop(Base):
    """The leveraged fund's stop level (README §7.2; task 5.1), one row per change: the latest
    is in force. Set when the fund is bought from flat, raised on each later E, never lowered;
    a split scales it (#116)."""

    __tablename__ = "fund_stops"
    __table_args__ = (
        CheckConstraint(_in("change", STOP_CHANGES), name="change"),
        CheckConstraint("level > 0", name="level"),
        Index("ix_fund_stops_symbol", "symbol", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    symbol: Mapped[str] = mapped_column(Text)
    session_date: Mapped[date] = mapped_column(Date)
    level: Mapped[float] = mapped_column(Double)
    change: Mapped[str] = mapped_column(Text)
    decision_price: Mapped[float | None] = mapped_column(Double)
    details: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'"))
    created_at: Mapped[datetime] = _created()


class BookMark(Base):
    """The twin's simulated account each session (README §5.5), in `dry_run` the book's own
    decisions replayed (SIM, #166), and SPY bought and held (the scorecard's third line, task
    4.4): rewritten whole each evening from the trial's start."""

    __tablename__ = "twin_book"
    __table_args__ = (CheckConstraint(_in("book", BOOKS), name="book"),)

    book: Mapped[str] = mapped_column(Text, primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    equity: Mapped[float] = mapped_column(Double)
    cash: Mapped[float] = mapped_column(Double)
    holdings: Mapped[dict[str, Any]] = mapped_column(JSONB)
    """{symbol: {"units": .., "value": ..}} at the close."""
    trades: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'"))
    halted: Mapped[bool] = mapped_column(Boolean)
    peak_equity: Mapped[float] = mapped_column(Double)
    tax_owed: Mapped[float] = mapped_column(Double, server_default=text("0"))
    """The estimated tax if everything were sold at the close: equity less it is "after tax"."""
    updated_at: Mapped[datetime] = _created()


class PortfolioJournal(Base):
    """One row per month (README §5.7): the decision and its reasons, the clips, the trades,
    and the month's results (task 4.4)."""

    __tablename__ = "portfolio_journal"

    execution_session: Mapped[date] = mapped_column(Date, primary_key=True)
    targets_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("portfolio_targets.id"))
    summary: Mapped[str] = mapped_column(Text)
    decision: Mapped[dict[str, Any]] = mapped_column(JSONB)
    clips: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'"))
    trades: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'"))
    results: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()

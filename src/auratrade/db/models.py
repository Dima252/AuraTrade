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
EARNINGS_TIMINGS = ("BEFORE_OPEN", "DURING_MARKET", "AFTER_CLOSE", "UNKNOWN")
JOB_STATUSES = (
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "SKIPPED_MARKET_CLOSED",
    "SKIPPED_NOT_SATURDAY",
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


class IndustryGroup(Base):
    __tablename__ = "industry_groups"

    id: Mapped[int] = mapped_column(SmallInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    # [[low, high], ...] inclusive SIC ranges that map to this group.
    sic_ranges: Mapped[list[list[int]]] = mapped_column(JSONB, server_default=text("'[]'"))
    rs_rank: Mapped[int | None] = mapped_column(SmallInteger)
    rs_rank_session: Mapped[date | None] = mapped_column(Date)
    updated_at: Mapped[datetime] = _created()


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
    cusip: Mapped[str | None] = mapped_column(String(9))
    cik: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str | None] = mapped_column(Text)
    exchange: Mapped[str | None] = mapped_column(Text)
    # COMMON, ADR, ETF, PREFERRED, ... as classified by ingest (#111).
    security_type: Mapped[str | None] = mapped_column(Text)
    sic: Mapped[int | None] = mapped_column(SmallInteger)
    industry_group_id: Mapped[int | None] = mapped_column(
        SmallInteger, ForeignKey("industry_groups.id")
    )
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    listed_at: Mapped[date | None] = mapped_column(Date)
    delisted_at: Mapped[date | None] = mapped_column(Date)
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


class Fundamental(Base):
    """One row per filing of a fiscal quarter, keyed on its EDGAR `filed` date (#99):
    backtests may read it only from the session after `filed_date`."""

    __tablename__ = "fundamentals"
    __table_args__ = (
        UniqueConstraint("ticker_id", "period_end", "filed_date"),
        CheckConstraint("fiscal_quarter BETWEEN 1 AND 4", name="fiscal_quarter"),
        CheckConstraint("filed_date >= period_end", name="filed_after_period"),
        Index("ix_fundamentals_ticker_filed", "ticker_id", "filed_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ticker_id: Mapped[int] = mapped_column(Integer, ForeignKey("tickers.id"))
    period_end: Mapped[date] = mapped_column(Date)
    fiscal_year: Mapped[int] = mapped_column(SmallInteger)
    fiscal_quarter: Mapped[int] = mapped_column(SmallInteger)
    filed_date: Mapped[date] = mapped_column(Date)
    form: Mapped[str | None] = mapped_column(Text)
    accession: Mapped[str | None] = mapped_column(Text)
    eps_diluted: Mapped[float | None] = mapped_column(Double)
    revenue: Mapped[int | None] = mapped_column(BigInteger)
    eps_growth: Mapped[float | None] = mapped_column(Double)
    revenue_growth: Mapped[float | None] = mapped_column(Double)
    eps_acceleration: Mapped[float | None] = mapped_column(Double)
    revenue_acceleration: Mapped[float | None] = mapped_column(Double)
    source: Mapped[str] = mapped_column(Text, server_default=text("'EDGAR'"))
    fetched_at: Mapped[datetime] = _created()


class EarningsEvent(Base):
    __tablename__ = "earnings_events"
    __table_args__ = (
        UniqueConstraint("ticker_id", "report_date", "source"),
        CheckConstraint(_in("timing", EARNINGS_TIMINGS), name="timing"),
        Index("ix_earnings_events_report_date", "report_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ticker_id: Mapped[int] = mapped_column(Integer, ForeignKey("tickers.id"))
    report_date: Mapped[date] = mapped_column(Date)
    timing: Mapped[str] = mapped_column(Text, server_default=text("'UNKNOWN'"))
    # EDGAR 8-K Item 2.02 acceptance time sets the timing for history (README §5.1).
    accepted_at: Mapped[datetime | None]
    eps_actual: Mapped[float | None] = mapped_column(Double)
    eps_estimate: Mapped[float | None] = mapped_column(Double)
    revenue_actual: Mapped[int | None] = mapped_column(BigInteger)
    revenue_estimate: Mapped[int | None] = mapped_column(BigInteger)
    surprise_pct: Mapped[float | None] = mapped_column(Double)
    source: Mapped[str] = mapped_column(Text)  # FINNHUB, EDGAR
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


BASE_STATUSES = ("FORMING", "BROKEN_OUT", "FAILED")


class DetectedBase(Base):
    """A base found by `indicators.bases` (README §5.3, task 2.2). The count is carried
    forward from these rows, so they outlive the 2-year bar window."""

    __tablename__ = "bases"
    __table_args__ = (
        UniqueConstraint("ticker_id", "start_date"),
        CheckConstraint(_in("status", BASE_STATUSES), name="status"),
        CheckConstraint("(end_date IS NULL) = (status = 'FORMING')", name="end_date"),
        CheckConstraint("(stage_number IS NULL) = (status = 'FAILED')", name="stage_number"),
        CheckConstraint("stage_number IS NULL OR stage_number >= 1", name="stage_positive"),
        CheckConstraint("end_date IS NULL OR end_date > start_date", name="ends_after_start"),
        CheckConstraint("low > 0 AND high >= low AND pivot > 0", name="prices"),
        CheckConstraint("depth >= 0 AND depth < 1", name="depth"),
        CheckConstraint("contraction_count >= 0", name="contraction_count"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ticker_id: Mapped[int] = mapped_column(Integer, ForeignKey("tickers.id"))
    start_date: Mapped[date] = mapped_column(Date)
    # The breakout session (BROKEN_OUT) or the session it became too deep (FAILED).
    end_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text)
    sessions: Mapped[int] = mapped_column(SmallInteger)
    high: Mapped[float] = mapped_column(Double)
    low: Mapped[float] = mapped_column(Double)
    low_date: Mapped[date] = mapped_column(Date)
    depth: Mapped[float] = mapped_column(Double)
    pivot: Mapped[float] = mapped_column(Double)
    contraction_count: Mapped[int] = mapped_column(SmallInteger)
    # [{"high_date", "high", "low_date", "low", "depth"}, ...], oldest first.
    contractions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'"))
    stage_number: Mapped[int | None] = mapped_column(SmallInteger)
    # False until the sequence is anchored at the IPO or a reset seen in the data; 3.5b
    # treats an unreliable count as stage 3 (half risk).
    count_reliable: Mapped[bool] = mapped_column(Boolean)
    advance: Mapped[float | None] = mapped_column(Double)
    reset: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    detected_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


REGIMES = ("CORRECTION", "EARLY_UPTREND", "UNDER_PRESSURE", "CONFIRMED_UPTREND")


class MarketRegimeRow(Base):
    """One row per session (README §5.2, task 2.3), written at the close; it governs the next
    session. `state` carries the state machine to the next evening. Kept forever (small)."""

    __tablename__ = "market_regime"
    __table_args__ = (
        CheckConstraint(_in("computed_regime", REGIMES), name="computed_regime"),
        CheckConstraint(_in("effective_regime", REGIMES), name="effective_regime"),
        CheckConstraint(f"llm_regime IS NULL OR {_in('llm_regime', REGIMES)}", name="llm_regime"),
        CheckConstraint(
            "index_trend BETWEEN -1 AND 1 AND breadth BETWEEN -1 AND 1 "
            "AND volatility BETWEEN -1 AND 1 AND distribution BETWEEN -1 AND 1 "
            "AND leadership BETWEEN -1 AND 1",
            name="pillars",
        ),
        CheckConstraint(
            "score = index_trend + breadth + volatility + distribution + leadership",
            name="score",
        ),
        CheckConstraint("sessions_in_state >= 1", name="sessions_in_state"),
        CheckConstraint("NOT no_ftd OR computed_regime = 'EARLY_UPTREND'", name="no_ftd"),
    )

    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    index_trend: Mapped[int] = mapped_column(SmallInteger)
    breadth: Mapped[int] = mapped_column(SmallInteger)
    volatility: Mapped[int] = mapped_column(SmallInteger)
    distribution: Mapped[int] = mapped_column(SmallInteger)
    leadership: Mapped[int] = mapped_column(SmallInteger)
    score: Mapped[int] = mapped_column(SmallInteger)
    computed_regime: Mapped[str] = mapped_column(Text)
    # The Portfolio Manager's view arrives in Phase 4; effective = the lower of the two.
    llm_regime: Mapped[str | None] = mapped_column(Text)
    effective_regime: Mapped[str] = mapped_column(Text)
    crisis: Mapped[bool] = mapped_column(Boolean)
    follow_through_day: Mapped[bool] = mapped_column(Boolean)
    no_ftd: Mapped[bool] = mapped_column(Boolean)
    sessions_in_state: Mapped[int] = mapped_column(SmallInteger)
    expiration_session: Mapped[bool] = mapped_column(Boolean)
    features: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'"))
    state: Mapped[dict[str, Any]]
    risk_hash: Mapped[str] = mapped_column(String(64))
    computed_at: Mapped[datetime] = _created()


# --- Position plans (task 3.5a, README §7.3, §10) -------------------------------------------
# These value lists mirror the enums in `auratrade.config` and `auratrade.plans` (a test
# checks they match), so the database rejects a value the code can't produce.

STRATEGIES = (
    "VCP_BREAKOUT",
    "MOMENTUM_BREAKOUT",
    "EPISODIC_PIVOT",
    "LEADER_PULLBACK",
    "EMERGING_LEADER",
)
TRAILS = ("SMA_50", "EMA_21", "TEN_DAY")
HOLD_STATES = ("OFF", "WATCHING", "ACTIVE", "ENDED")
PLAN_CHANGES = (
    "ENTRY",
    "TRANCHE_2",
    "MILESTONE",
    "PARTIAL_SALE",
    "EIGHT_WEEK_HOLD",
    "REGIME_DOWNGRADE",
    "EARNINGS_CUSHION",
    "TIGHTEN",
    "SPLIT",
)
PLAN_RULES = (
    "HARD_STOP",
    "EARNINGS_CUSHION",
    "HEAVY_VOLUME_REVERSAL",
    "CLIMAX",
    "LATE_STAGE",
    "REGIME_DOWNGRADE",
    "EIGHT_WEEK_HOLD",
    "STRENGTH_PARTIAL",
    "TRAIL",
    "TIME_STOP",
)
POSITION_EVENT_KINDS = (
    "EXIT",
    "SALE",
    "STOP_RAISED",
    "TRAIL_TIGHTENED",
    "HOLD_STARTED",
    "HOLD_ENDED",
    "REGIME_DOWNGRADE",
    "EARNINGS_CUSHION",
    "CLIMAX_SIGNALS",
    "FALTERING_RS",
    "FAILED_BREAKOUT",
)


class PositionPlanRow(Base):
    """One version of a position's plan (`plans.model.PositionPlan`), tighten-only. Kept
    forever (README §10)."""

    __tablename__ = "position_plans"
    __table_args__ = (
        UniqueConstraint("position_id", "version"),
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint(_in("strategy", STRATEGIES), name="strategy"),
        CheckConstraint(_in("change", PLAN_CHANGES), name="change"),
        CheckConstraint(_in("trail", TRAILS), name="trail"),
        CheckConstraint(_in("hold_state", HOLD_STATES), name="hold_state"),
        CheckConstraint("pivot > 0 AND initial_stop > 0", name="positive"),
        CheckConstraint("initial_stop < entry_price", name="initial_stop"),
        CheckConstraint("hard_stop >= initial_stop", name="hard_stop"),
        CheckConstraint(
            "(hold_state = 'ACTIVE') = (hold_until_session IS NOT NULL)", name="hold_until"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # `positions` comes with the order layer; the foreign key is added with it.
    position_id: Mapped[int] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(SmallInteger)
    ticker_id: Mapped[int] = mapped_column(Integer, ForeignKey("tickers.id"))
    strategy: Mapped[str] = mapped_column(Text)
    change: Mapped[str] = mapped_column(Text)
    # The session whose review (or the entry) wrote this version.
    session_date: Mapped[date] = mapped_column(Date)
    pivot: Mapped[float] = mapped_column(Double)
    entry_price: Mapped[float] = mapped_column(Double)
    initial_stop: Mapped[float] = mapped_column(Double)
    hard_stop: Mapped[float] = mapped_column(Double)
    # The trail in force (the 8-week hold's while it is active).
    trail: Mapped[str] = mapped_column(Text)
    hold_state: Mapped[str] = mapped_column(Text)
    hold_until_session: Mapped[int | None] = mapped_column(SmallInteger)
    # The whole plan version, authoritative; the columns above are for queries and checks.
    plan: Mapped[dict[str, Any]]
    # `plans.levels.plan_levels`: tomorrow's hard stop, trail level and milestone triggers.
    levels: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'"))
    risk_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()


class PositionEventRow(Base):
    """Milestones, sales, climax signals, faltering RS, time stops, the cushion, downgrades."""

    __tablename__ = "position_events"
    __table_args__ = (
        CheckConstraint(_in("kind", POSITION_EVENT_KINDS), name="kind"),
        CheckConstraint(f"rule IS NULL OR {_in('rule', PLAN_RULES)}", name="rule"),
        CheckConstraint("shares IS NULL OR shares > 0", name="shares"),
        Index("ix_position_events_position_session", "position_id", "session_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    position_id: Mapped[int] = mapped_column(BigInteger)
    ticker_id: Mapped[int] = mapped_column(Integer, ForeignKey("tickers.id"))
    session_date: Mapped[date] = mapped_column(Date)
    plan_version: Mapped[int | None] = mapped_column(SmallInteger)
    kind: Mapped[str] = mapped_column(Text)
    rule: Mapped[str | None] = mapped_column(Text)
    shares: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'"))
    created_at: Mapped[datetime] = _created()

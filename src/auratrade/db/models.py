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
    # Finnhub's profile industry: the group for filers without a SIC code (spike 0.3e).
    finnhub_industry: Mapped[str | None] = mapped_column(Text)
    industry_group_id: Mapped[int | None] = mapped_column(
        SmallInteger, ForeignKey("industry_groups.id")
    )
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # The first public trading date (an OTC uplisting is not an IPO, #126); NULL when it is
    # unknown or before the stored history.
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
    # True when the vendor left the timing blank and it came from the company's usual timing
    # over its last reports, or the before-the-open default (#119).
    timing_inferred: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    fiscal_year: Mapped[int | None] = mapped_column(SmallInteger)
    fiscal_quarter: Mapped[int | None] = mapped_column(SmallInteger)
    # History: the earlier of the 8-K Item 2.02 acceptance and the first earnings headline
    # sets the timing (README §5.1, #119).
    accepted_at: Mapped[datetime | None]
    headline_at: Mapped[datetime | None]
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
    """A base found by `indicators.bases` (archived README §5.3, task 2.2). The count is carried
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


# --- Position plans (task 3.5a, archived README §7.3; README §10) ---------------------------------
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
    position_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("positions.id"))
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
    position_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("positions.id"))
    ticker_id: Mapped[int] = mapped_column(Integer, ForeignKey("tickers.id"))
    session_date: Mapped[date] = mapped_column(Date)
    plan_version: Mapped[int | None] = mapped_column(SmallInteger)
    kind: Mapped[str] = mapped_column(Text)
    rule: Mapped[str | None] = mapped_column(Text)
    shares: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'"))
    created_at: Mapped[datetime] = _created()


# --- The order layer (task 1.7, README §5.7, §10) ----------------------------------------------

# Who decided a signal: the walking skeleton's rule-only pick (task 1.7). Phase 5 adds the
# Portfolio Manager. Kept apart so "your numbers" and the go-live statistics never count the
# skeleton's trades.
SOURCES = ("SKELETON",)
SIGNAL_STATUSES = ("PENDING", "SUBMITTED", "FILLED", "CANCELED", "SKIPPED", "EXPIRED")
POSITION_STATUSES = ("OPEN", "CLOSED")
ORDER_PURPOSES = ("ENTRY", "STOP_LEG", "STOP", "EXIT", "OTHER")
# The plan rules, plus a position closed outside the order layer (by hand at the broker).
EXIT_REASONS = (*PLAN_RULES, "MANUAL")


class Signal(Base):
    """The queue (README §5.7): one entry order to place, decided the evening before."""

    __tablename__ = "signals"
    __table_args__ = (
        CheckConstraint(_in("source", SOURCES), name="source"),
        CheckConstraint(_in("strategy", STRATEGIES), name="strategy"),
        CheckConstraint(_in("status", SIGNAL_STATUSES), name="status"),
        CheckConstraint("action IN ('ENTRY')", name="action"),
        CheckConstraint("tranche IN (1, 2)", name="tranche"),
        CheckConstraint(
            "0 < stop_price AND stop_price < trigger_price AND trigger_price <= limit_price",
            name="levels",
        ),
        CheckConstraint("shares IS NULL OR shares > 0", name="shares"),
        Index("ix_signals_session", "session_date", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    # The session it trades on, and the one whose data it was decided from.
    session_date: Mapped[date] = mapped_column(Date)
    decided_session: Mapped[date] = mapped_column(Date)
    ticker_id: Mapped[int] = mapped_column(Integer, ForeignKey("tickers.id"))
    symbol: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    strategy: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, server_default=text("'ENTRY'"))
    tranche: Mapped[int] = mapped_column(SmallInteger, server_default=text("1"))
    rank: Mapped[int] = mapped_column(SmallInteger)
    pivot: Mapped[float] = mapped_column(Double)
    trigger_price: Mapped[float] = mapped_column(Double)
    limit_price: Mapped[float] = mapped_column(Double)
    stop_price: Mapped[float] = mapped_column(Double)
    risk_fraction: Mapped[float] = mapped_column(Double)
    shares: Mapped[int | None] = mapped_column(Integer)
    client_order_id: Mapped[str | None] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, server_default=text("'PENDING'"))
    status_reason: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class Position(Base):
    """A position as the broker holds it; `shares` mirrors the broker's quantity."""

    __tablename__ = "positions"
    __table_args__ = (
        CheckConstraint(_in("source", SOURCES), name="source"),
        CheckConstraint(_in("strategy", STRATEGIES), name="strategy"),
        CheckConstraint(_in("status", POSITION_STATUSES), name="status"),
        CheckConstraint(
            f"exit_reason IS NULL OR {_in('exit_reason', EXIT_REASONS)}", name="exit_reason"
        ),
        CheckConstraint(
            f"exit_pending IS NULL OR {_in('exit_pending', EXIT_REASONS)}", name="exit_pending"
        ),
        CheckConstraint("shares >= 0 AND max_shares > 0", name="shares"),
        CheckConstraint("(status = 'CLOSED') = (closed_session IS NOT NULL)", name="closed"),
        # One open position per symbol: reconciliation keys on it.
        Index(
            "uq_positions_open_symbol",
            "symbol",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ticker_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("tickers.id"))
    symbol: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    strategy: Mapped[str] = mapped_column(Text)
    signal_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("signals.id"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'OPEN'"))
    shares: Mapped[int] = mapped_column(Integer)
    max_shares: Mapped[int] = mapped_column(Integer)
    entry_price: Mapped[float] = mapped_column(Double)
    initial_stop: Mapped[float] = mapped_column(Double)
    opened_session: Mapped[date] = mapped_column(Date)
    closed_session: Mapped[date | None] = mapped_column(Date)
    regime_at_entry: Mapped[str | None] = mapped_column(Text)
    # A sale the plan decided that hasn't happened yet (no fill, a closed market): the next
    # order job with the market open retries it (README §5.7.2, #124).
    exit_pending: Mapped[str | None] = mapped_column(Text)
    exit_pending_since: Mapped[datetime | None]
    exit_reason: Mapped[str | None] = mapped_column(Text)
    opened_at: Mapped[datetime] = _created()
    closed_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = _created()


class OrderRow(Base):
    """The broker's orders as last seen (README §10). Legs carry their parent's id."""

    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(_in("purpose", ORDER_PURPOSES), name="purpose"),
        Index("ix_orders_symbol_created", "symbol", "created_at"),
        Index("ix_orders_position", "position_id"),
    )

    broker_id: Mapped[str] = mapped_column(Text, primary_key=True)
    client_order_id: Mapped[str] = mapped_column(Text)
    parent_broker_id: Mapped[str | None] = mapped_column(Text)
    replaces: Mapped[str | None] = mapped_column(Text)
    position_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("positions.id"))
    signal_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("signals.id"))
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


class TradeJournalRow(Base):
    """One row per closed trade (archived README §5.10; the book's `book_journal` replaces it).
    The skeleton fills the basics; the sim R, default-plan R and MAE/MFE come with the simulator
    (3.6) and the full journal (5.5)."""

    __tablename__ = "trade_journal"
    __table_args__ = (
        CheckConstraint(_in("source", SOURCES), name="source"),
        CheckConstraint(_in("exit_reason", EXIT_REASONS), name="exit_reason"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    position_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("positions.id"), unique=True)
    ticker_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("tickers.id"))
    symbol: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    strategy: Mapped[str] = mapped_column(Text)
    entry_session: Mapped[date] = mapped_column(Date)
    exit_session: Mapped[date] = mapped_column(Date)
    holding_sessions: Mapped[int] = mapped_column(SmallInteger)
    shares: Mapped[int] = mapped_column(Integer)
    entry_price: Mapped[float] = mapped_column(Double)
    exit_price: Mapped[float | None] = mapped_column(Double)
    initial_stop: Mapped[float] = mapped_column(Double)
    pnl: Mapped[float | None] = mapped_column(Double)
    r_multiple: Mapped[float | None] = mapped_column(Double)
    exit_reason: Mapped[str] = mapped_column(Text)
    regime_at_entry: Mapped[str | None] = mapped_column(Text)
    regime_at_exit: Mapped[str | None] = mapped_column(Text)
    mae_r: Mapped[float | None] = mapped_column(Double)
    mfe_r: Mapped[float | None] = mapped_column(Double)
    sim_r: Mapped[float | None] = mapped_column(Double)
    default_plan_r: Mapped[float | None] = mapped_column(Double)
    details: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'"))
    created_at: Mapped[datetime] = _created()


class AccountSnapshot(Base):
    """Equity at each session's `post_market` (README §5.10); the risk multiplier comes in
    Phase 5."""

    __tablename__ = "account_snapshots"

    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    equity: Mapped[float] = mapped_column(Double)
    cash: Mapped[float] = mapped_column(Double)
    long_market_value: Mapped[float] = mapped_column(Double)
    peak_equity: Mapped[float] = mapped_column(Double)
    drawdown: Mapped[float] = mapped_column(Double)
    open_positions: Mapped[int] = mapped_column(SmallInteger)
    risk_multiplier: Mapped[float | None] = mapped_column(Double)
    details: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'"))
    created_at: Mapped[datetime] = _created()


# --- Watchlist, baseline book, breakout tracker, scorecard (task 3.7, archived README §5.12) --

TRADE_OUTCOMES = ("SKIPPED", "MISSED", "OPEN", "CLOSED")
BASELINE_STATUSES = ("REJECTED", *TRADE_OUTCOMES)
VERDICTS = ("BUY", "WATCH", "SKIP")


class WatchlistRow(Base):
    """Session T's full, uncapped scan (archived README §5.3): every setup with its score;
    ``rank`` is its place in the capped list (None: not selected). The breakout tracker reads
    every row, the baseline book the ranked ones. 90 days in Postgres, then Parquet (README §10)."""

    __tablename__ = "daily_watchlist"
    __table_args__ = (
        UniqueConstraint("session_date", "ticker_id", "strategy"),
        CheckConstraint(_in("strategy", STRATEGIES), name="strategy"),
        CheckConstraint(_in("regime", REGIMES), name="regime"),
        CheckConstraint("0 < structural_stop AND structural_stop < pivot", name="levels"),
        CheckConstraint("rank IS NULL OR rank >= 1", name="rank"),
        Index("ix_daily_watchlist_session_rank", "session_date", "rank"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date)
    ticker_id: Mapped[int] = mapped_column(Integer, ForeignKey("tickers.id"))
    symbol: Mapped[str] = mapped_column(Text)
    strategy: Mapped[str] = mapped_column(Text)
    pivot: Mapped[float] = mapped_column(Double)
    structural_stop: Mapped[float] = mapped_column(Double)
    # What a re-review is keyed on (`strategies.verdict_cache.structure`).
    structure: Mapped[float] = mapped_column(Double)
    score: Mapped[float] = mapped_column(Double)
    rank: Mapped[int | None] = mapped_column(SmallInteger)
    # The computed regime the selection used (it governs session T+1).
    regime: Mapped[str] = mapped_column(Text)
    # What the guardrails and the simulator read, as of session T.
    close: Mapped[float] = mapped_column(Double)
    atr: Mapped[float | None] = mapped_column(Double)
    adr: Mapped[float | None] = mapped_column(Double)
    sma_50: Mapped[float | None] = mapped_column(Double)
    dollar_volume: Mapped[float | None] = mapped_column(Double)
    base_stage: Mapped[int | None] = mapped_column(SmallInteger)
    base_reliable: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    industry: Mapped[str | None] = mapped_column(Text)
    components: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'"))
    # The features and the strategy's own facts (the Analyst's compact context, 4.3).
    facts: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'"))
    chart_keys: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'"))
    risk_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()


class BreakoutRow(Base):
    """A trigger of the full scan, ungated, and its result 5 sessions later (README §5.12);
    the leadership-health pillar reads the complete ones. 90 days in Postgres (README §10)."""

    __tablename__ = "breakout_tracker"
    __table_args__ = (
        UniqueConstraint("ticker_id", "strategy", "trigger_session"),
        CheckConstraint(_in("strategy", STRATEGIES), name="strategy"),
        CheckConstraint(
            "(result_session IS NULL) = (above_trigger IS NULL) "
            "AND (result_session IS NULL) = (result_close IS NULL)",
            name="result",
        ),
        CheckConstraint("pivot > 0 AND trigger_close > 0", name="prices"),
        Index("ix_breakout_tracker_trigger_session", "trigger_session"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ticker_id: Mapped[int] = mapped_column(Integer, ForeignKey("tickers.id"))
    symbol: Mapped[str] = mapped_column(Text)
    strategy: Mapped[str] = mapped_column(Text)
    # The scan that listed it (T-1) and the session that triggered it (T).
    watchlist_session: Mapped[date] = mapped_column(Date)
    trigger_session: Mapped[date] = mapped_column(Date)
    pivot: Mapped[float] = mapped_column(Double)
    trigger_close: Mapped[float] = mapped_column(Double)
    result_session: Mapped[date | None] = mapped_column(Date)
    result_close: Mapped[float | None] = mapped_column(Double)
    above_trigger: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class BaselineSignalRow(Base):
    """One shadow-book proposal (archived README §5.12): rejected by the guardrails, or approved and
    simulated from its entry session through ``sim/``, re-played each evening. Kept forever."""

    __tablename__ = "baseline_signals"
    __table_args__ = (
        UniqueConstraint("session_date", "ticker_id"),
        CheckConstraint(_in("strategy", STRATEGIES), name="strategy"),
        CheckConstraint(_in("regime", REGIMES), name="regime"),
        CheckConstraint(_in("status", BASELINE_STATUSES), name="status"),
        CheckConstraint("(status = 'REJECTED') = (risk_amount IS NULL)", name="approved"),
        CheckConstraint(f"exit_rule IS NULL OR {_in('exit_rule', PLAN_RULES)}", name="exit_rule"),
        Index("ix_baseline_signals_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # The scan session whose list it came from (T-1) and its entry session (T).
    decided_session: Mapped[date] = mapped_column(Date)
    session_date: Mapped[date] = mapped_column(Date)
    ticker_id: Mapped[int] = mapped_column(Integer, ForeignKey("tickers.id"))
    symbol: Mapped[str] = mapped_column(Text)
    strategy: Mapped[str] = mapped_column(Text)
    rank: Mapped[int] = mapped_column(SmallInteger)
    conviction: Mapped[int] = mapped_column(SmallInteger)
    regime: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    # The guardrails' findings (clips, or the rejection), then the 09:36 check's.
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'"))
    pivot: Mapped[float] = mapped_column(Double)
    stop: Mapped[float] = mapped_column(Double)
    risk_amount: Mapped[float | None] = mapped_column(Double)
    # The approved entry (`guardrails.evaluate.ApprovedEntry`) and the simulator's inputs.
    entry: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'"))
    # The simulation so far (`sim.trade.TradeResult`).
    sessions_simulated: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    shares: Mapped[int | None] = mapped_column(Integer)
    risk_dollars: Mapped[float | None] = mapped_column(Double)
    pnl: Mapped[float | None] = mapped_column(Double)
    r_multiple: Mapped[float | None] = mapped_column(Double)
    exit_session: Mapped[date | None] = mapped_column(Date)
    exit_rule: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'"))
    risk_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class VerdictOutcomeRow(Base):
    """One scorecard unit (archived README §5.12): a setup (ticker, strategy, structure)
    labelled with the verdict in force at its trigger, or its last verdict, played forward under
    the default plan. Small, kept forever."""

    __tablename__ = "verdict_outcomes"
    __table_args__ = (
        UniqueConstraint("ticker_id", "strategy", "structure"),
        CheckConstraint(_in("strategy", STRATEGIES), name="strategy"),
        CheckConstraint(_in("verdict", VERDICTS), name="verdict"),
        CheckConstraint(f"outcome IS NULL OR {_in('outcome', TRADE_OUTCOMES)}", name="outcome"),
        CheckConstraint("triggered = (trigger_session IS NOT NULL)", name="triggered"),
        CheckConstraint("reviews >= 1", name="reviews"),
        CheckConstraint(f"exit_rule IS NULL OR {_in('exit_rule', PLAN_RULES)}", name="exit_rule"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ticker_id: Mapped[int] = mapped_column(Integer, ForeignKey("tickers.id"))
    symbol: Mapped[str] = mapped_column(Text)
    strategy: Mapped[str] = mapped_column(Text)
    structure: Mapped[float] = mapped_column(Double)
    # The labelling verdict and its tags.
    verdict: Mapped[str] = mapped_column(Text)
    conviction: Mapped[int | None] = mapped_column(SmallInteger)
    verdict_session: Mapped[date] = mapped_column(Date)
    prompt_version: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    reviews: Mapped[int] = mapped_column(SmallInteger)
    first_review_session: Mapped[date] = mapped_column(Date)
    last_review_session: Mapped[date] = mapped_column(Date)
    triggered: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    trigger_session: Mapped[date | None] = mapped_column(Date)
    regime_at_trigger: Mapped[str | None] = mapped_column(Text)
    # The last attempt's outcome: SKIPPED or MISSED before a fill, OPEN or CLOSED after.
    outcome: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    # R after each scorecard horizon, keyed by its sessions ("5", "10", "20").
    r_after: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'"))
    r_final: Mapped[float | None] = mapped_column(Double)
    exit_rule: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()

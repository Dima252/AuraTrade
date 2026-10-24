"""The evening `simulate` job's work (README §3, §5.12), on session T's bars: the breakout
tracker, the shadow baseline book and the verdict scorecard.

Every simulated trade is re-played each evening from its entry session through ``sim/``, from
the minute bars cached in the object store (``sim.minutes``) and the daily bars in Postgres, so
a re-run of an evening writes the same rows and nothing depends on state carried between runs.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Connection, Select, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from auratrade.calendar.sessions import MarketCalendar
from auratrade.config import Regime, RiskConfig, Strategy
from auratrade.db.models import (
    BaselineSignalRow,
    BreakoutRow,
    DailyBar,
    EarningsEvent,
    IndustryGroup,
    MarketRegimeRow,
    Ticker,
    VerdictOutcomeRow,
    WatchlistRow,
)
from auratrade.guardrails.blocks import EntryBlock, loss_halts
from auratrade.guardrails.evaluate import ApprovedEntry, Context, Holding, SetupFacts
from auratrade.indicators.bars import Bar
from auratrade.journal import baseline
from auratrade.journal.breakout_tracker import (
    Breakout,
    Tracked,
    Watched,
    above_trigger,
    for_pillar,
    triggers,
)
from auratrade.journal.records import StoredEntry, finding_json, result_from_json, result_json
from auratrade.journal.replay import sized_entry
from auratrade.journal.scorecard import Review, in_force, units
from auratrade.plans.cushion import EarningsTiming
from auratrade.regime.pillars import TrackedBreakout
from auratrade.regime.state import early_uptrend_stepped
from auratrade.regime.store import read_state
from auratrade.sim.minutes import MinuteCache
from auratrade.sim.sessions import CalendarDay, Market, Minutes, SymbolDays, sim_sessions
from auratrade.sim.trade import FillKind, TradeOutcome, TradeResult, TradeSetup, simulate_trade

BENCHMARK = "SPY"
HISTORY_SESSIONS = 260
"""Daily bars before an entry: the 200-day, the trails and the 52-week RS line."""
REPORT_HORIZON = timedelta(days=200)
"""Known report dates loaded past T, for the cushion and the blackout."""


# ---- loading ------------------------------------------------------------------------------


def objects[M](conn: Connection, stmt: Select[M]) -> list[M]:
    """ORM rows on the job's connection (read only: the session never flushes)."""
    with Session(bind=conn) as session:
        return list(session.scalars(stmt))


def sessions_back(calendar: MarketCalendar, day: date, n: int) -> date:
    """``n`` sessions before ``day``, or the calendar's first session if it is shorter."""
    if calendar.first is None:
        raise ValueError("market_sessions is empty: sync the calendar")
    days = calendar.sessions_between(calendar.first, day)
    return days[max(len(days) - 1 - n, 0)].day


def industries(conn: Connection, ticker_ids: Iterable[int]) -> dict[int, str]:
    rows = conn.execute(
        select(Ticker.id, IndustryGroup.name)
        .join(IndustryGroup, IndustryGroup.id == Ticker.industry_group_id)
        .where(Ticker.id.in_(sorted(set(ticker_ids))))
    ).all()
    return {int(t): str(name) for t, name in rows}


def load_market(conn: Connection, calendar: MarketCalendar, start: date, last: date) -> Market:
    days = [
        CalendarDay(s.day, s.open_at, s.close_at, s.is_early_close)
        for s in calendar.sessions_between(start, last)
    ]
    rows = conn.execute(
        select(
            MarketRegimeRow.session_date, MarketRegimeRow.computed_regime, MarketRegimeRow.crisis
        ).where(MarketRegimeRow.session_date.between(start, last))
    ).all()
    benchmark = dict(
        conn.execute(
            select(DailyBar.session_date, DailyBar.close)
            .join(Ticker, Ticker.id == DailyBar.ticker_id)
            .where(Ticker.symbol == BENCHMARK, Ticker.active)
            .where(DailyBar.session_date.between(start, last))
        ).all()
    )
    return Market(
        calendar=days,
        regimes={d: Regime(r) for d, r, _ in rows},
        crises={d: c for d, _, c in rows},
        benchmark=benchmark,
    )


def load_days(
    conn: Connection, ticker_ids: Iterable[int], start: date, last: date
) -> dict[int, SymbolDays]:
    ids = sorted(set(ticker_ids))
    bars: dict[int, list[Any]] = defaultdict(list)
    for row in conn.execute(
        select(
            DailyBar.ticker_id,
            DailyBar.session_date,
            DailyBar.open,
            DailyBar.high,
            DailyBar.low,
            DailyBar.close,
            DailyBar.volume,
        )
        .where(DailyBar.ticker_id.in_(ids), DailyBar.session_date.between(start, last))
        .order_by(DailyBar.ticker_id, DailyBar.session_date)
    ).all():
        bars[row.ticker_id].append(row)
    reports: dict[int, set[tuple[date, EarningsTiming]]] = defaultdict(set)
    for ticker_id, day, timing in conn.execute(
        select(EarningsEvent.ticker_id, EarningsEvent.report_date, EarningsEvent.timing).where(
            EarningsEvent.ticker_id.in_(ids),
            EarningsEvent.report_date.between(start, last + REPORT_HORIZON),
        )
    ).all():
        reports[ticker_id].add((day, EarningsTiming(timing)))
    return {
        t: SymbolDays(
            dates=[b.session_date for b in bars[t]],
            bars=[Bar(b.open, b.high, b.low, b.close, float(b.volume)) for b in bars[t]],
            reports=sorted(reports[t]),
        )
        for t in ids
    }


# ---- re-playing stored trades -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Stored:
    """One stored trade to re-play from ``entry_session``."""

    ticker_id: int
    symbol: str
    entry_session: date
    stored: StoredEntry


@dataclass
class Replayer:
    """Re-plays stored trades over one evening's loaded data; minute bars are fetched once per
    session for every symbol that needs them."""

    market: Market
    days: Mapping[int, SymbolDays]
    cache: MinuteCache
    risk: RiskConfig
    _minutes: dict[date, dict[str, Minutes]] = field(default_factory=dict)

    def sessions(self, first: date, last: date) -> list[date]:
        return [c.day for c in self.market.calendar if first <= c.day <= last]

    def prefetch(self, trades: Iterable[Stored], last: date) -> None:
        wanted: dict[date, set[str]] = defaultdict(set)
        for t in trades:
            for day in self.sessions(t.entry_session, last):
                wanted[day].add(t.symbol)
        times = {c.day: c for c in self.market.calendar}
        for day, symbols in sorted(wanted.items()):
            have = self._minutes.setdefault(day, {})
            missing = sorted(symbols - have.keys())
            if missing:
                c = times[day]
                have.update(
                    self.cache.minutes(day, missing, open_at=c.open_at, close_at=c.close_at)
                )

    def replay(self, trade: Stored, last: date) -> TradeResult:
        days = self.sessions(trade.entry_session, last)
        symbol_days = self.days.get(trade.ticker_id, SymbolDays([], []))
        minutes = {
            d: m for d in days if (m := self._minutes.get(d, {}).get(trade.symbol)) is not None
        }
        if trade.entry_session not in minutes:
            return TradeResult(TradeOutcome.SKIPPED)
        cut = bisect_left(list(symbol_days.dates), trade.entry_session)
        history = list(symbol_days.bars[max(cut - HISTORY_SESSIONS, 0) : cut])
        if not history:
            return TradeResult(TradeOutcome.SKIPPED)
        s = trade.stored
        sessions = sim_sessions(
            s.entry.strategy,
            days,
            symbol=symbol_days,
            minutes=minutes,
            market=self.market,
            risk=self.risk,
            tested_ma=s.tested_ma,
        )
        if not sessions:
            return TradeResult(TradeOutcome.SKIPPED)
        setup = TradeSetup(
            entry=s.entry,
            code_pivot=s.code_pivot,
            atr=s.atr,
            adr=s.adr,
            dollar_volume=s.dollar_volume,
            history=history,
        )
        return simulate_trade(setup, sessions, equity=s.equity, record=s.record, risk=self.risk)


def shares_at(result: TradeResult, session: int) -> int:
    """Shares held after session ``session`` of the trade (1 = the entry session)."""
    held = 0
    for f in result.fills:
        if f.session <= session:
            held += f.shares if f.kind in (FillKind.ENTRY, FillKind.TRANCHE_2) else -f.shares
    return held


# ---- the breakout tracker -----------------------------------------------------------------


def track_breakouts(
    conn: Connection, calendar: MarketCalendar, session: date, risk: RiskConfig
) -> dict[str, int]:
    """Session T's triggers of T-1's full scan, and every result that is now complete."""
    prev = calendar.previous_session(session).day
    listed = conn.execute(
        select(
            WatchlistRow.ticker_id, WatchlistRow.symbol, WatchlistRow.strategy, WatchlistRow.pivot
        ).where(WatchlistRow.session_date == prev)
    ).all()
    ids = {r.symbol: r.ticker_id for r in listed}
    bars = {
        symbol: Bar(o, h, lo, c, float(v))
        for symbol, o, h, lo, c, v in conn.execute(
            select(
                Ticker.symbol,
                DailyBar.open,
                DailyBar.high,
                DailyBar.low,
                DailyBar.close,
                DailyBar.volume,
            )
            .join(Ticker, Ticker.id == DailyBar.ticker_id)
            .where(DailyBar.session_date == session, DailyBar.ticker_id.in_(ids.values()))
        ).all()
    }
    found = triggers(
        [Watched(r.symbol, Strategy(r.strategy), r.pivot) for r in listed], bars, session
    )
    if found:
        conn.execute(
            pg_insert(BreakoutRow)
            .values(
                [
                    {
                        "ticker_id": ids[b.symbol],
                        "symbol": b.symbol,
                        "strategy": b.strategy.value,
                        "watchlist_session": prev,
                        "trigger_session": b.session,
                        "pivot": b.pivot,
                        "trigger_close": b.close,
                    }
                    for b in found
                ]
            )
            .on_conflict_do_nothing()
        )
    return {"triggers": len(found), "results": complete_results(conn, calendar, session, risk)}


def complete_results(
    conn: Connection, calendar: MarketCalendar, session: date, risk: RiskConfig
) -> int:
    """Fill in every pending result whose session has come (catching up a missed evening)."""
    n = risk.regime.leadership.result_sessions
    pending = objects(conn, select(BreakoutRow).where(BreakoutRow.result_session.is_(None)))
    done = 0
    for row in pending:
        days = calendar.sessions_between(row.trigger_session, session)
        if len(days) <= n:
            continue
        result_day = days[n].day
        closes = dict(
            conn.execute(
                select(DailyBar.session_date, DailyBar.close).where(
                    DailyBar.ticker_id == row.ticker_id,
                    DailyBar.session_date.between(row.trigger_session, result_day),
                )
            ).all()
        )
        # Every result must complete (the pillar refuses a window with a missing one): without
        # the trigger bar there is nothing to rescale by, and without any bar since, the close
        # the breakout recorded is the last one known.
        then = closes.get(row.trigger_session, row.trigger_close)
        result_close = closes[max(closes)] if closes else row.trigger_close
        conn.execute(
            update(BreakoutRow)
            .where(BreakoutRow.id == row.id)
            .values(
                result_session=result_day,
                result_close=result_close,
                above_trigger=above_trigger(
                    _as_breakout(row), close_then_now=then, result_close=result_close
                ),
                updated_at=func.now(),
            )
        )
        done += 1
    return done


def _as_breakout(row: BreakoutRow) -> Breakout:
    return Breakout(
        row.symbol, Strategy(row.strategy), row.pivot, row.trigger_session, row.trigger_close
    )


def tracked_for_regime(
    conn: Connection, calendar: MarketCalendar, session: date, risk: RiskConfig
) -> list[TrackedBreakout]:
    """The tracker rows the leadership-health pillar reads for ``session``."""
    cfg = risk.regime.leadership
    window = cfg.result_sessions + cfg.window_sessions
    first = sessions_back(calendar, session, window - 1)
    sessions = [s.day for s in calendar.sessions_between(first, session)]
    rows = conn.execute(
        select(BreakoutRow.trigger_session, BreakoutRow.above_trigger).where(
            BreakoutRow.trigger_session.between(first, session)
        )
    ).all()
    return for_pillar([Tracked(d, a) for d, a in rows], sessions, cfg)


# ---- the baseline book --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BookRow:
    id: int
    ticker_id: int
    symbol: str
    session_date: date
    status: str
    conviction: int
    stored: StoredEntry
    result: TradeResult | None


def _book_rows(conn: Connection, before: date) -> list[BookRow]:
    rows = objects(
        conn,
        select(BaselineSignalRow)
        .where(BaselineSignalRow.session_date < before, BaselineSignalRow.status != "REJECTED")
        .order_by(BaselineSignalRow.session_date, BaselineSignalRow.rank),
    )
    return [
        BookRow(
            r.id,
            r.ticker_id,
            r.symbol,
            r.session_date,
            r.status,
            r.conviction,
            StoredEntry.from_json(r.entry),
            result_from_json(r.result) if r.result else None,
        )
        for r in rows
    ]


def _book_trades(rows: Sequence[BookRow], market: Market) -> list[baseline.BookTrade]:
    days = [c.day for c in market.calendar]
    return [
        baseline.BookTrade(
            r.stored.entry.strategy,
            days[bisect_left(days, r.session_date) :],
            r.result,
        )
        for r in rows
        if r.result is not None and r.result.triggered
    ]


def _blocks(
    start_equity: float,
    trades: Sequence[baseline.BookTrade],
    book_days: Sequence[date],
    *,
    early_close: bool,
    crisis: bool,
    risk: RiskConfig,
) -> frozenset[EntryBlock]:
    """The book's entry blocks: a half day, the crisis override, and its own loss halts (the
    drawdown halt isn't latched: no one re-enables a shadow book by hand)."""
    blocks: set[EntryBlock] = set()
    if early_close:
        blocks.add(EntryBlock.EARLY_CLOSE)
    if crisis:
        blocks.add(EntryBlock.CRISIS_OVERRIDE)
    if book_days:
        curve = [baseline.equity_on(start_equity, trades, d) for d in book_days]
        now = curve[-1]
        day_before = curve[-2] if len(curve) > 1 else start_equity
        week_before = curve[-6] if len(curve) > 5 else start_equity
        peak = max(start_equity, *curve)
        blocks |= loss_halts(
            day_return=now / day_before - 1,
            week_return=now / week_before - 1,
            drawdown=1 - now / peak,
            config=risk.halts,
        )
    return frozenset(blocks)


def _sessions_to(days: Sequence[date], start: date, end: date) -> int:
    """Sessions from ``start`` to ``end``: 0 on the same session, negative when before."""
    return bisect_left(days, end) - bisect_left(days, start)


def _report_distances(
    reports: Sequence[tuple[date, EarningsTiming]], session: date, days: Sequence[date]
) -> tuple[int | None, int | None]:
    upcoming = [d for d, _ in reports if d >= session]
    past = [d for d, _ in reports if d < session]
    next_in = _sessions_to(days, session, min(upcoming)) if upcoming else None
    last_ago = _sessions_to(days, max(past), session) if past else None
    return next_in, last_ago


def run_baseline(
    conn: Connection,
    calendar: MarketCalendar,
    session: date,
    risk: RiskConfig,
    cache: MinuteCache,
) -> dict[str, Any]:
    """Decide session T's entries from T-1's list, then simulate them and every open trade
    through T."""
    prev = calendar.previous_session(session).day
    regime_row = conn.execute(
        select(
            MarketRegimeRow.computed_regime, MarketRegimeRow.score, MarketRegimeRow.crisis
        ).where(MarketRegimeRow.session_date == prev)
    ).one_or_none()
    if regime_row is None:
        return {"skipped": f"no computed regime for {prev}"}
    rows = _book_rows(conn, session)
    start = sessions_back(calendar, min([r.session_date for r in rows] + [prev]), HISTORY_SESSIONS)
    market = load_market(conn, calendar, start, session)
    calendar_days = [c.day for c in market.calendar]

    listed = objects(
        conn,
        select(WatchlistRow)
        .where(WatchlistRow.session_date == prev, WatchlistRow.rank.is_not(None))
        .order_by(WatchlistRow.rank),
    )
    ids = {r.ticker_id for r in rows} | {r.ticker_id for r in listed}
    days = load_days(conn, ids, start, session)
    groups = industries(conn, ids)
    replayer = Replayer(market, days, cache, risk)
    correlation = risk.guardrails.cluster_correlation_sessions

    # The book as it stood at T-1's close.
    trades = _book_trades(rows, market)
    first_day = min((r.session_date for r in rows), default=session)
    book_days = [d for d in calendar_days if first_day <= d <= prev]
    start_equity = risk.baseline.start_equity
    equity = baseline.equity_on(start_equity, trades, prev)
    held_rows = [
        r
        for r in rows
        if r.session_date <= prev
        and r.result is not None
        and r.result.triggered
        and (r.status == "OPEN" or _exit_after(r, market, prev))
    ]
    replayer.prefetch(
        [Stored(r.ticker_id, r.symbol, r.session_date, r.stored) for r in held_rows], prev
    )
    holdings = []
    for r in held_rows:
        at_prev = replayer.replay(Stored(r.ticker_id, r.symbol, r.session_date, r.stored), prev)
        if at_prev.outcome is not TradeOutcome.OPEN or at_prev.plan is None:
            continue
        held_days = _sessions_to(calendar_days, r.session_date, prev) + 1
        holdings.append(
            Holding(
                symbol=r.symbol,
                shares=shares_at(at_prev, held_days),
                price=_close_on(days[r.ticker_id], prev),
                plan=at_prev.plan,
                industry=groups.get(r.ticker_id),
                closes=_closes_to(days[r.ticker_id], prev, correlation),
                session=held_days + 1,
                conviction=r.conviction,
            )
        )

    regime = Regime(regime_row.computed_regime)
    state = read_state(conn, prev)
    step = risk.regimes.early_uptrend_step
    stepped = state is not None and early_uptrend_stepped(
        state,
        regime_row.score,
        starter_sessions=step.starter_sessions,
        min_score=step.min_score_to_step_up,
    )
    today = calendar.session_for(session)
    blocks = _blocks(
        start_equity,
        trades,
        book_days,
        early_close=bool(today and today.is_early_close),
        crisis=regime_row.crisis,
        risk=risk,
    )
    facts = {}
    items = []
    for w in listed:
        sd = days.get(w.ticker_id, SymbolDays([], []))
        next_in, last_ago = _report_distances(sd.reports, session, calendar_days)
        f = SetupFacts(
            symbol=w.symbol,
            strategy=Strategy(w.strategy),
            pivot=w.pivot,
            structural_stop=w.structural_stop,
            price=w.close,
            atr=w.atr,
            adr=w.adr,
            sma_50=w.sma_50,
            base_stage=w.base_stage,
            base_reliable=w.base_reliable,
            industry=w.industry,
            closes=_closes_to(sd, prev, correlation),
            next_report_in=next_in,
            last_report_ago=last_ago,
        )
        facts[(f.symbol, f.strategy)] = f
        items.append(baseline.Listed(f, w.score))
    multiplier = baseline.multiplier(start_equity, trades, book_days, risk)
    records = baseline.records(
        [t for t in trades if t.exit_session is not None and t.exit_session <= prev], risk
    )
    context = Context(
        equity=equity,
        regime=regime,
        early_uptrend_stepped=stepped,
        multiplier=multiplier,
        blocks=blocks,
        setups=facts,
        holdings={h.symbol: h for h in holdings},
        records=records,
    )
    evaluation = baseline.decide(items, context, risk)

    conn.execute(delete(BaselineSignalRow).where(BaselineSignalRow.session_date == session))
    new: list[tuple[int, Stored]] = []
    convictions = baseline.convictions(items)
    for w, conviction, decision in zip(listed, convictions, evaluation.decisions, strict=True):
        approved = decision.approved if isinstance(decision.approved, ApprovedEntry) else None
        stored = (
            StoredEntry(
                approved,
                code_pivot=w.pivot,
                atr=w.atr,
                adr=w.adr,
                dollar_volume=w.dollar_volume,
                equity=equity,
                record=records.get(approved.strategy),
                tested_ma=_tested_ma(w),
            )
            if approved is not None
            else None
        )
        row_id = conn.execute(
            insert(BaselineSignalRow)
            .values(
                decided_session=prev,
                session_date=session,
                ticker_id=w.ticker_id,
                symbol=w.symbol,
                strategy=w.strategy,
                rank=w.rank,
                conviction=conviction,
                regime=regime.value,
                status="REJECTED" if stored is None else "MISSED",
                findings=[finding_json(f) for f in decision.findings],
                pivot=w.pivot,
                stop=w.structural_stop,
                risk_amount=None if approved is None else approved.risk_amount,
                entry={} if stored is None else stored.to_json(),
                risk_hash=risk.config_hash,
            )
            .returning(BaselineSignalRow.id)
        ).scalar_one()
        if stored is not None:
            new.append((row_id, Stored(w.ticker_id, w.symbol, session, stored)))

    open_rows = [
        (r.id, Stored(r.ticker_id, r.symbol, r.session_date, r.stored))
        for r in rows
        if r.status == "OPEN" or _exit_after(r, market, prev)
    ]
    to_play = open_rows + new
    replayer.prefetch([t for _, t in to_play], session)
    statuses: dict[str, int] = defaultdict(int)
    for row_id, trade in to_play:
        result = replayer.replay(trade, session)
        _write_result(conn, row_id, result, market, trade.entry_session)
        statuses[result.outcome.value] += 1
    return {
        "listed": len(listed),
        "approved": len(new),
        "equity": round(equity, 2),
        "multiplier": multiplier,
        "blocks": sorted(blocks),
        "simulated": dict(statuses),
    }


def _closes_to(days: SymbolDays, day: date, n: int) -> list[float]:
    """The last ``n`` closes up to ``day``."""
    cut = bisect_left(list(days.dates), day)
    if cut < len(days.dates) and days.dates[cut] == day:
        cut += 1
    return [b.close for b in days.bars[max(cut - n, 0) : cut]]


def _close_on(days: SymbolDays, day: date) -> float:
    i = bisect_left(list(days.dates), day)
    if i < len(days.dates) and days.dates[i] == day:
        return days.bars[i].close
    return days.bars[max(i - 1, 0)].close


def _tested_ma(w: WatchlistRow) -> float | None:
    value = w.facts.get("tested_ema") if w.facts else None
    return float(value) if isinstance(value, (int, float)) else None


def _exit_after(r: BookRow, market: Market, day: date) -> bool:
    if r.result is None or r.result.exit is None:
        return False
    days = [c.day for c in market.calendar if c.day >= r.session_date]
    exit_index = len(r.result.marks) - 1
    return exit_index < len(days) and days[exit_index] > day


def _write_result(
    conn: Connection, row_id: int, result: TradeResult, market: Market, entry: date
) -> None:
    days = [c.day for c in market.calendar if c.day >= entry]
    exit_session = days[len(result.marks) - 1] if result.exit is not None else None
    conn.execute(
        update(BaselineSignalRow)
        .where(BaselineSignalRow.id == row_id)
        .values(
            status=result.outcome.value,
            sessions_simulated=len(result.marks),
            shares=shares_at(result, len(result.marks)) if result.triggered else None,
            risk_dollars=result.risk if result.triggered else None,
            pnl=result.pnl if result.triggered else None,
            r_multiple=result.r,
            exit_session=exit_session,
            exit_rule=None if result.exit is None else result.exit.value,
            result=result_json(result),
            updated_at=func.now(),
        )
    )


# ---- the verdict scorecard ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TickerReview:
    """A verdict with the ticker it was given on (verdicts arrive in Phase 4: `setups`)."""

    ticker_id: int
    review: Review


def score_verdicts(
    conn: Connection,
    calendar: MarketCalendar,
    session: date,
    risk: RiskConfig,
    cache: MinuteCache,
    reviews: Sequence[TickerReview],
) -> dict[str, int]:
    """Every unit's attempt on session T while a verdict is in force, and the forward R of
    the triggered ones up to the last horizon (README §5.12)."""
    if not reviews:
        return {"units": 0}
    card = risk.scorecard
    prev = calendar.previous_session(session).day
    ticker_of = {r.review.unit: r.ticker_id for r in reviews}
    grouped = units(r.review for r in reviews)
    first = min(r.review.session for r in reviews)
    start = sessions_back(calendar, first, HISTORY_SESSIONS)
    market = load_market(conn, calendar, start, session)
    calendar_days = [c.day for c in market.calendar]
    position = {d: i for i, d in enumerate(calendar_days)}
    days = load_days(conn, ticker_of.values(), start, session)
    replayer = Replayer(market, days, cache, risk)
    existing = {
        (r.ticker_id, r.strategy, r.structure): r for r in objects(conn, select(VerdictOutcomeRow))
    }
    listed = {
        (w.ticker_id, w.strategy, w.structure): w
        for w in objects(conn, select(WatchlistRow).where(WatchlistRow.session_date == prev))
    }
    max_age = risk.ranking.verdict_max_age_sessions
    horizon = max(card.horizons)
    plays: list[tuple[tuple[int, str, float], Stored, Review | None]] = []
    for unit, unit_reviews in grouped.items():
        ticker_id = ticker_of[unit]
        key = (ticker_id, unit[1].value, unit[2])
        row = existing.get(key)
        if row is not None and row.triggered and row.trigger_session is not None:
            done = (
                row.outcome == "CLOSED"
                or _sessions_to(calendar_days, row.trigger_session, session) >= horizon
            )
            if not done:
                stored = StoredEntry.from_json(row.details["entry"])
                plays.append((key, Stored(ticker_id, unit[0], row.trigger_session, stored), None))
            continue
        verdict = in_force(unit_reviews, session, position, max_age)
        w = listed.get(key)
        if verdict is None or w is None or w.adr is None:
            _upsert_unit(conn, key, unit_reviews, None, None, attempt=None)
            continue
        entry = sized_entry(
            symbol=w.symbol,
            strategy=Strategy(w.strategy),
            pivot=w.pivot,
            stop=w.structural_stop,
            code_pivot=w.pivot,
            adr=w.adr,
            risk_fraction=card.risk_per_trade,
            equity=card.notional_equity,
            risk=risk,
        )
        if entry is None:
            _upsert_unit(conn, key, unit_reviews, None, None, attempt=None)
            continue
        stored = StoredEntry(
            entry,
            code_pivot=w.pivot,
            atr=w.atr,
            adr=w.adr,
            dollar_volume=w.dollar_volume,
            equity=card.notional_equity,
            record=None,
            tested_ma=_tested_ma(w),
        )
        plays.append((key, Stored(ticker_id, unit[0], session, stored), verdict))
    replayer.prefetch([p for _, p, _ in plays], session)
    counted: dict[str, int] = defaultdict(int)
    for key, trade, verdict in plays:
        result = replayer.replay(trade, session)
        unit_reviews = grouped[(trade.symbol, Strategy(key[1]), key[2])]
        if verdict is not None and not result.triggered:
            _upsert_unit(conn, key, unit_reviews, None, None, attempt=result.outcome)
            counted["attempts"] += 1
            continue
        label = verdict if verdict is not None else _label(existing[key], unit_reviews)
        regime = market.regimes.get(prev) if verdict is not None else None
        _upsert_unit(
            conn,
            key,
            unit_reviews,
            label,
            (trade, result),
            attempt=None,
            regime=regime,
            horizons=card.horizons,
        )
        counted["triggered" if verdict is not None else "followed"] += 1
    counted["units"] = len(grouped)
    return dict(counted)


def _label(row: VerdictOutcomeRow, reviews: Sequence[Review]) -> Review:
    for r in reviews:
        if r.session == row.verdict_session:
            return r
    return reviews[-1]


def _upsert_unit(
    conn: Connection,
    key: tuple[int, str, float],
    reviews: Sequence[Review],
    label: Review | None,
    played: tuple[Stored, TradeResult] | None,
    *,
    attempt: TradeOutcome | None,
    regime: Regime | None = None,
    horizons: Sequence[int] = (),
) -> None:
    ticker_id, strategy, structure = key
    tag = label or reviews[-1]
    values: dict[str, Any] = {
        "ticker_id": ticker_id,
        "symbol": tag.symbol,
        "strategy": strategy,
        "structure": structure,
        "verdict": tag.verdict.value,
        "conviction": tag.conviction,
        "verdict_session": tag.session,
        "prompt_version": tag.prompt_version,
        "model": tag.model,
        "reviews": len(reviews),
        "first_review_session": reviews[0].session,
        "last_review_session": reviews[-1].session,
        "updated_at": func.now(),
    }
    if played is not None:
        trade, result = played
        values |= {
            "triggered": True,
            "trigger_session": trade.entry_session,
            "outcome": result.outcome.value,
            "r_after": {str(h): v for h in horizons if (v := result.r_after(h)) is not None},
            "r_final": result.r if result.exit is not None else None,
            "exit_rule": None if result.exit is None else result.exit.value,
            "details": {"entry": trade.stored.to_json(), "result": result_json(result)},
        }
        if regime is not None:
            values["regime_at_trigger"] = regime.value
    stmt = pg_insert(VerdictOutcomeRow).values(
        **values,
        attempts=1 if attempt is not None else 0,
        **({"outcome": attempt.value} if attempt is not None else {}),
    )
    update_values: dict[str, Any] = {
        k: stmt.excluded[k] for k in values if k not in ("ticker_id", "strategy", "structure")
    }
    if attempt is not None:
        update_values["attempts"] = VerdictOutcomeRow.attempts + 1
        update_values["outcome"] = stmt.excluded.outcome
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["ticker_id", "strategy", "structure"], set_=update_values
        )
    )

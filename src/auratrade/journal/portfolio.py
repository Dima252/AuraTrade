"""The portfolio manager's evening steps on the database (README §5.3-§5.5; task 4.3, #166).

- **The funds' bars** (split-adjusted and total-return, free Alpaca calls) for the core funds
  and the satellite list, from ``history_sessions`` before the trial's start to T.
- **The books:** the twin (the rules on the whole account), SPY bought and held, and, in
  `dry_run`, the book itself (its decided targets replayed, #166), all through
  `sim/portfolio.py` from the trial's first R, written whole to `twin_book` each evening
  (`simulate`), with the scorecard (README §5.5, `journal/portfolio_scorecard.py`) in each
  month's `portfolio_journal` row.
- **On R** (`allocate`): the trend, the cap and the book's halt from the same runs (ranked at
  R), the rule level, the twin's targets, and the Portfolio Manager's facts, in an ALLOCATED
  `portfolio_targets` row for E. On `paper` the halt is the account's (`execution.halt`),
  turned off here on a DOWN -> UP flip or a new peak; in dry_run it is the simulated book's.

The trial starts at the first R `allocate` ever ran on; both books start there from
``dry_run_equity`` in cash. Until task 5.x trades the paper account, the simulated book *is* the
account the Portfolio Manager sees.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from sqlalchemy import Connection, delete, func, insert, orm, select, update

from auratrade.calendar.rebalance import Rebalance, rebalances
from auratrade.calendar.sessions import MarketCalendar
from auratrade.config import RiskConfig
from auratrade.data.alpaca_data import Adjustment, DailyBar
from auratrade.db.models import (
    BookMark,
    LlmDecision,
    MacroObservation,
    MarketRegimeRow,
    PortfolioJournal,
    PortfolioTarget,
)
from auratrade.execution import halt as live_halt
from auratrade.journal.portfolio_scorecard import (
    Decision,
    Marks,
    Scorecard,
    period_json,
    score,
)
from auratrade.llm.portfolio_facts import (
    AccountFacts,
    PastDecision,
    PmFacts,
    TrendFacts,
    etf_facts,
    facts_to_json,
)
from auratrade.sim.funds import funds_market
from auratrade.sim.portfolio import BENCHMARK, Market, PortfolioRun, Rules, simulate_portfolio
from auratrade.strategies import core_allocation
from auratrade.strategies.core_allocation import (
    Trend,
    realized_volatility,
    sma,
    twin_targets,
)
from auratrade.tax.israel import Lot

TWIN, SIM, SPY = "TWIN", "SIM", "SPY"
DRY_RUN_NOTE = (
    "dry_run: no orders are sent, so the account is your decided targets replayed through the "
    "simulator from the trial's start (#166)."
)


class FundBars(Protocol):
    def daily_bars(
        self, symbols: Sequence[str], start: date, end: date, *, adjustment: Adjustment = ...
    ) -> dict[str, list[DailyBar]]: ...


def fund_symbols(risk: RiskConfig) -> list[str]:
    p = risk.portfolio
    return sorted({p.market, p.leveraged, p.cash, *p.satellite_list})


@dataclass(frozen=True)
class FundData:
    bars: dict[str, list[DailyBar]]
    """Split-adjusted, for charts."""
    total_return: dict[str, list[DailyBar]]

    def closes(self, symbol: str, *, total_return: bool = False) -> dict[date, float]:
        bars = (self.total_return if total_return else self.bars).get(symbol, [])
        return {b.session: b.close for b in bars}

    def market(self, risk: RiskConfig, start: date) -> Market:
        symbols = sorted(self.bars)
        return funds_market(
            {s: self.closes(s) for s in symbols},
            {s: self.closes(s, total_return=True) for s in symbols},
            risk.portfolio,
            start,
        )


def fetch_funds(source: FundBars, risk: RiskConfig, start: date, end: date) -> FundData:
    symbols = fund_symbols(risk)
    split = source.daily_bars(symbols, start, end, adjustment=Adjustment.SPLIT)
    tr = source.daily_bars(symbols, start, end, adjustment=Adjustment.ALL)
    missing = sorted(set(symbols) - (split.keys() & tr.keys()))
    if missing:
        raise ValueError(f"no bars for {missing}")
    return FundData(split, tr)


def trial_start(conn: Connection) -> date | None:
    """The first R `allocate` ran on: where both books start."""
    first: date | None = conn.scalar(
        select(func.min(PortfolioTarget.ranking_session)).where(PortfolioTarget.kind == "REBALANCE")
    )
    return first


def window_start(calendar: MarketCalendar, start: date, risk: RiskConfig) -> date:
    return calendar.n_sessions_back(start, risk.portfolio_manager.history_sessions).day


def decided_targets(conn: Connection) -> dict[date, dict[str, float]]:
    rows = conn.execute(
        select(PortfolioTarget.execution_session, PortfolioTarget.targets).where(
            PortfolioTarget.kind == "REBALANCE", PortfolioTarget.status == "DECIDED"
        )
    )
    return {day: dict(targets) for day, targets in rows if targets is not None}


@dataclass(frozen=True)
class Books:
    market: Market
    twin: PortfolioRun
    sim: PortfolioRun
    spy: PortfolioRun
    """The market fund bought at the first E and held: the scorecard's third line."""


def run_books(
    market: Market,
    risk: RiskConfig,
    start: date,
    end: date,
    book: Mapping[date, Mapping[str, float]],
    *,
    rank_at_end: bool = False,
) -> Books:
    """The twin, the book and SPY from ``start`` (the trial's first R) through ``end``'s
    close."""
    at = {d: i for i, d in enumerate(market.days)}
    if start not in at or end not in at:
        raise ValueError(f"the funds' bars don't cover {start} to {end}")
    p = risk.portfolio
    kwargs: dict[str, Any] = {
        "start": at[start],
        "end": at[end],
        "equity": risk.portfolio_manager.dry_run_equity,
        "tax_rate": p.tax_rate,
        "sell_at_end": False,
        "rank_at_end": rank_at_end,
    }
    twin = simulate_portfolio(market, Rules(), p, **kwargs)
    sim = simulate_portfolio(market, Rules(), p, book=book, **kwargs)
    spy = simulate_portfolio(market, BENCHMARK, p, **kwargs)
    return Books(market, twin, sim, spy)


def write_books(conn: Connection, books: Books) -> dict[str, Any]:
    """Rewrite `twin_book` whole from the runs (each evening's runs restart at the trial)."""
    conn.execute(delete(BookMark))
    out: dict[str, Any] = {}
    for name, run in ((TWIN, books.twin), (SIM, books.sim), (SPY, books.spy)):
        trades: dict[date, list[dict[str, Any]]] = defaultdict(list)
        for t in run.trades:
            trades[t.day].append(
                {"symbol": t.symbol, "units": t.units, "price": t.price, "reason": t.reason}
            )
        at = {d: i for i, d in enumerate(books.market.days)}
        rows = []
        for day, mark, snap in zip(run.days, run.marks, run.snapshots, strict=True):
            i = at[day]
            holdings = {
                s: {"units": u, "value": u * books.market.prices[s][i]}
                for s, u in sorted(snap.units.items())
            }
            rows.append(
                {
                    "book": name,
                    "session_date": day,
                    "equity": mark,
                    "cash": snap.cash,
                    "holdings": holdings,
                    "trades": trades.get(day, []),
                    "halted": snap.halted,
                    "peak_equity": snap.peak,
                    "tax_owed": snap.tax_owed,
                }
            )
        if rows:
            conn.execute(insert(BookMark), rows)
        out[name] = {"sessions": len(rows), "equity": round(run.marks[-1], 2) if rows else None}
    return out


def marks(run: PortfolioRun) -> Marks:
    return Marks(
        run.days,
        run.marks,
        [s.tax_owed for s in run.snapshots],
        [s.realized for s in run.snapshots],
    )


def scored_decisions(conn: Connection, market: str) -> list[Decision]:
    """The decided rebalances with the expectations the LLM stated (none when it held)."""
    rows = conn.execute(
        select(
            PortfolioTarget.execution_session,
            PortfolioTarget.level,
            PortfolioTarget.rule_level,
            PortfolioTarget.satellite,
            PortfolioTarget.held_satellite,
            LlmDecision.answer,
        )
        .outerjoin(LlmDecision, LlmDecision.id == PortfolioTarget.llm_decision_id)
        .where(PortfolioTarget.kind == "REBALANCE", PortfolioTarget.status == "DECIDED")
    )
    out = []
    for day, level, rule_level, satellite, held, answer in rows:
        stated = None if held else (answer or {}).get("satellite")
        # Answers before prompt pm-2 carry no number to score.
        expectations = {
            w["symbol"]: w["expected_vs_market"]
            for w in stated or ()
            if w.get("expected_vs_market") is not None and w["symbol"] != market
        }
        shift = (level if level is not None else rule_level) - rule_level
        out.append(Decision(day, shift, dict(satellite or {}), expectations))
    return out


def scorecard(conn: Connection, risk: RiskConfig, data: FundData, books: Books) -> Scorecard:
    p = risk.portfolio
    return score(
        marks(books.sim),
        marks(books.twin),
        marks(books.spy),
        scored_decisions(conn, p.market),
        {s: data.closes(s, total_return=True) for s in sorted(data.total_return)},
        market=p.market,
        cash=p.cash,
        satellite_share=p.satellite_share,
        tax_rate=p.tax_rate,
        cfg=risk.portfolio_manager.scorecard,
    )


def write_scorecard(conn: Connection, card: Scorecard, books: Books) -> dict[str, Any]:
    """Each month's results and the book's trades on its E into its journal row."""
    trades: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for t in books.sim.trades:
        trades[t.day].append(
            {"symbol": t.symbol, "units": t.units, "price": t.price, "reason": t.reason}
        )
    for period in card.periods:
        conn.execute(
            update(PortfolioJournal)
            .where(PortfolioJournal.execution_session == period.session)
            .values(
                results=finite(period_json(period)),
                trades=trades.get(period.session, []),
                updated_at=func.now(),
            )
        )
    summary = card.summary()
    return {
        "through": str(card.through),
        "months_complete": int(summary["months_complete"]),
        "excess_after_tax": round(summary["excess_after_tax"], 6),
    }


def rebalance_at(calendar: MarketCalendar, ranking: date) -> Rebalance | None:
    """R's rebalance, if ``ranking`` is an R (the last session of its month): the sessions
    from R to the next month's first full session, through `calendar.rebalance`."""
    session = calendar.session_for(ranking)
    if session is None:
        return None
    sessions = [session]
    while sessions[-1].day.month == ranking.month or sessions[-1].is_early_close:
        sessions.append(calendar.next_session(sessions[-1].day))
    return next((r for r in rebalances(sessions) if r.ranking == ranking), None)


def month_end_values(market: Market, upto: int) -> list[float]:
    """The total-return index at each month's last session through session ``upto`` (R)."""
    days = market.days
    ends = [i for i in range(upto) if days[i + 1].month != days[i].month]
    return [market.index[i] for i in [*ends, upto]]


def build_facts(
    conn: Connection,
    risk: RiskConfig,
    data: FundData,
    books: Books,
    ranking: date,
    session: date,
    *,
    halted: bool | None = None,
) -> PmFacts:
    """The facts on R for E, from the book ranked at R (`run_books(rank_at_end=True)`);
    `halted` is the account's halt where it isn't the simulated book's."""
    p, pm = risk.portfolio, risk.portfolio_manager
    market, sim = books.market, books.sim
    i = market.days.index(ranking)
    r = sim.rankings[-1]
    if r.day != ranking or sim.end is None:
        raise ValueError(f"the book wasn't ranked at {ranking}")
    month_ends = month_end_values(market, i)
    average = sma(month_ends, p.sma_months)
    if average is None:
        raise ValueError(f"{ranking}: fewer than {p.sma_months} month ends for the trend")
    volatility = realized_volatility(market.returns[max(0, i + 1 - p.vol_sessions) : i + 1], p)
    trend = TrendFacts(
        r.state,
        month_ends[-1],
        average,
        volatility,
        r.capped,
        r.halted if halted is None else halted,
    )

    end = sim.end
    equity = sim.marks[-1]
    prices = {s: market.prices[s][i] for s in market.prices}
    weights = {s: u * prices[s] / equity for s, u in end.units.items()}
    cash = end.cash + end.accrued
    if cash > 0:
        weights[p.cash] = weights.get(p.cash, 0.0) + cash / equity
    lots: dict[str, Sequence[Lot]] = {s: end.tax.lots(s) for s in end.tax.symbols()}
    account = AccountFacts(
        equity=equity,
        peak_equity=end.halt.peak,
        weights=weights,
        previous_satellite=previous_satellite(conn, session),
        lots=lots,
        prices=prices,
        realized_this_year=end.tax.realized(session.year),
        loss_carried=end.tax.carry,
        satellite_tax_this_year=satellite_tax_this_year(conn, session),
    )
    market_tr = [data.closes(p.market, total_return=True)[d] for d in market.days[: i + 1]]
    etfs = []
    for symbol in p.satellite_list:
        tr = data.closes(symbol, total_return=True)
        # A missing bar carries the last close (as the market does).
        closes: list[float] = []
        for d in market.days[: i + 1]:
            closes.append(tr.get(d, closes[-1] if closes else math.nan))
        known = [c for c in closes if math.isfinite(c)]
        etfs.append(etf_facts(symbol, known, market_tr[-len(known) :], pm))
    return PmFacts(
        session=session,
        ranking_session=ranking,
        trend=trend,
        account=account,
        etfs=etfs,
        regime=regime_fact(conn, ranking),
        macro=macro_facts(conn, ranking),
        headlines=(),
        history=past_decisions(conn, session, pm.history_months),
        scorecard=finite(scorecard(conn, risk, data, books).summary()),
        notes=(DRY_RUN_NOTE,),
    )


def previous_satellite(conn: Connection, before: date) -> dict[str, float]:
    row = conn.scalar(
        select(PortfolioTarget.satellite)
        .where(
            PortfolioTarget.status == "DECIDED",
            PortfolioTarget.execution_session < before,
            PortfolioTarget.satellite.is_not(None),
        )
        .order_by(PortfolioTarget.execution_session.desc(), PortfolioTarget.id.desc())
        .limit(1)
    )
    return dict(row) if row else {}


def satellite_tax_this_year(conn: Connection, before: date) -> float:
    total = conn.scalar(
        select(func.coalesce(func.sum(PortfolioTarget.satellite_tax), 0.0)).where(
            PortfolioTarget.status == "DECIDED",
            PortfolioTarget.execution_session < before,
            PortfolioTarget.execution_session >= date(before.year, 1, 1),
        )
    )
    return float(total or 0.0)


def regime_fact(conn: Connection, upto: date) -> dict[str, Any] | None:
    row = (
        orm.Session(conn)
        .scalars(
            select(MarketRegimeRow)
            .where(MarketRegimeRow.session_date <= upto)
            .order_by(MarketRegimeRow.session_date.desc())
            .limit(1)
        )
        .one_or_none()
    )
    if row is None:
        return None
    return {
        "session": str(row.session_date),
        "regime": row.computed_regime,
        "score": row.score,
        "pillars": {
            "index_trend": row.index_trend,
            "breadth": row.breadth,
            "volatility": row.volatility,
            "distribution": row.distribution,
            "leadership": row.leadership,
        },
        "sessions_in_state": row.sessions_in_state,
        "note": "a descriptive classifier; no rule reads it",
    }


def macro_facts(conn: Connection, upto: date) -> dict[str, float]:
    latest = (
        select(
            MacroObservation.series,
            func.max(MacroObservation.observation_date).label("day"),
        )
        .where(MacroObservation.observation_date <= upto)
        .group_by(MacroObservation.series)
        .subquery()
    )
    rows = conn.execute(
        select(MacroObservation.series, MacroObservation.value).join(
            latest,
            (MacroObservation.series == latest.c.series)
            & (MacroObservation.observation_date == latest.c.day),
        )
    )
    return dict(rows.all())


def past_decisions(conn: Connection, before: date, months: int) -> list[PastDecision]:
    rows = (
        orm.Session(conn)
        .execute(
            select(PortfolioTarget, LlmDecision.answer, PortfolioJournal.results)
            .outerjoin(LlmDecision, LlmDecision.id == PortfolioTarget.llm_decision_id)
            .outerjoin(
                PortfolioJournal,
                PortfolioJournal.execution_session == PortfolioTarget.execution_session,
            )
            .where(
                PortfolioTarget.status == "DECIDED",
                PortfolioTarget.kind == "REBALANCE",
                PortfolioTarget.execution_session < before,
            )
            .order_by(PortfolioTarget.execution_session.desc())
            .limit(months)
        )
        .all()
    )
    out = []
    for target, answer, results in reversed(rows):
        summary = (answer or {}).get("summary") or (
            "held (the LLM failed)" if target.fell_back else ""
        )
        out.append(
            PastDecision(
                target.execution_session,
                round((target.level or target.rule_level) - target.rule_level, 4),
                None if target.held_satellite else target.satellite,
                summary,
                ((results or {}).get("after_tax") or {}).get("excess"),
                bool(target.fell_back),
            )
        )
    return out


def allocate(
    conn: Connection,
    calendar: MarketCalendar,
    risk: RiskConfig,
    source: FundBars,
    ranking: date,
    *,
    live: bool = False,
    write_halt: bool = True,
) -> dict[str, Any]:
    """On R: the rule level, the twin's targets and the facts in an ALLOCATED row for E.
    `live`: the halt is the account's (`paper`), checked here first; `write_halt` turns it off
    in `system_flags` when the check says so."""
    rebalance = rebalance_at(calendar, ranking)
    if rebalance is None:
        return {"skipped": "not a ranking session"}
    existing = conn.execute(
        select(PortfolioTarget.status).where(
            PortfolioTarget.kind == "REBALANCE",
            PortfolioTarget.execution_session == rebalance.execution,
        )
    ).scalar_one_or_none()
    if existing == "DECIDED":
        return {"skipped": f"E {rebalance.execution} is already decided"}
    start = trial_start(conn) or ranking
    data = fetch_funds(source, risk, window_start(calendar, start, risk), ranking)
    market = data.market(risk, window_start(calendar, start, risk))
    books = run_books(market, risk, start, ranking, decided_targets(conn), rank_at_end=True)
    r, twin = books.sim.rankings[-1], books.twin.rankings[-1]
    halted, level, halt_check = r.halted, r.level, None
    if live:
        flipped_up = r.flipped and r.state is Trend.UP
        halt_check = live_halt.check_on_ranking(
            conn, ranking, flipped_up, risk.portfolio, write=write_halt
        )
        halted = bool(halt_check["on"])
        level = core_allocation.rule_level(r.state, r.capped, halted, risk.portfolio)
    facts = build_facts(
        conn, risk, data, books, ranking, rebalance.execution, halted=halted if live else None
    )
    values = {
        "kind": "REBALANCE",
        "ranking_session": ranking,
        "execution_session": rebalance.execution,
        "status": "ALLOCATED",
        "trend": r.state.value,
        "capped": r.capped,
        "halted": halted,
        "rule_level": level,
        "twin_targets": twin_targets(twin.level, risk.portfolio),
        "facts": finite(facts_to_json(facts)),
        "risk_hash": risk.config_hash,
    }
    if existing is None:
        conn.execute(insert(PortfolioTarget).values(**values))
    else:
        conn.execute(
            update(PortfolioTarget)
            .where(
                PortfolioTarget.kind == "REBALANCE",
                PortfolioTarget.execution_session == rebalance.execution,
            )
            .values(**values)
        )
    out: dict[str, Any] = {
        "ranking": str(ranking),
        "execution": str(rebalance.execution),
        "trend": r.state.value,
        "capped": r.capped,
        "halted": halted,
        "rule_level": level,
        "trial_start": str(start),
    }
    if halt_check is not None:
        out["halt"] = halt_check
    return out


def simulate_books(
    conn: Connection,
    calendar: MarketCalendar,
    risk: RiskConfig,
    source: FundBars,
    session: date,
) -> dict[str, Any]:
    """The books through ``session``'s close into `twin_book`, and the scorecard into the
    journal."""
    start = trial_start(conn)
    if start is None or start > session:
        return {"skipped": "the trial hasn't started"}
    first = window_start(calendar, start, risk)
    data = fetch_funds(source, risk, first, session)
    # On R the market ends before E, so R is ranked only when asked (its targets are E's).
    is_ranking = rebalance_at(calendar, session) is not None
    books = run_books(
        data.market(risk, first),
        risk,
        start,
        session,
        decided_targets(conn),
        rank_at_end=is_ranking,
    )
    out = write_books(conn, books)
    out["scorecard"] = write_scorecard(conn, scorecard(conn, risk, data, books), books)
    return out


def finite(value: Any) -> Any:
    """JSON for Postgres: a non-finite number (which JSONB rejects) becomes null."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {k: finite(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [finite(v) for v in value]
    return value

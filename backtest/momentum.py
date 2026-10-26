"""The momentum book's backtest (tasks 3.13-3.16; `docs/backtests/momentum-preregistration.md`).

    uv run python -m backtest.momentum --blind     # 3.14: counts, invariants -> momentum-blind.md
    uv run python -m backtest.momentum             # 3.15: the one scored run -> momentum.md
    uv run python -m backtest.momentum --holdout   # 3.16: the frozen configuration, once

Everything is fixed in the pre-registration, committed before any of this read real data.
**The holdout stays closed:** nothing after `TUNING_END` is loaded unless ``--holdout``, which
refuses to run unless `risk.yaml` hashes to `FROZEN_HASH` (set when 3.16 freezes it).

- **The market:** the Parquet history (`backtest.history_scan`), with the regime of the 5-pillar
  variant `CHOSEN` replayed from 2016 (the overlay), the crisis override from the same run,
  SPY's split-adjusted 200-session SMA (C4), the EDGAR reports' reaction sessions (C5), the
  history's SIC groups (current, not point in time) and each security's successor: the next
  piece of the same ticker (the pre-registration's §3.8), when it is loaded.
- **Configurations:** the primary, C1-C7 (one change each), the adopted configuration when
  something is adopted, and the diagnostics (timing offsets, $100k notional, zero cost).
- **Benchmarks:** SPY and MTUM total-return closes (`history.benchmarks`).
"""

from __future__ import annotations

import argparse
import bisect
import pickle
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

from auratrade.calendar.rebalance import rebalances
from auratrade.calendar.sessions import Session
from auratrade.config import (
    REPO_ROOT,
    BookOverlay,
    BookSignal,
    Regime,
    RiskConfig,
    load_risk_config,
)
from auratrade.history.read import SecuritySeries
from auratrade.indicators.earnings import EarningsTiming, reaction_session
from auratrade.regime.classifier import RegimeResult
from auratrade.sim.book import BookMarket, BookRun, BookSeries, ranking_on, simulate_book
from backtest import momentum_report as mr
from backtest.history_scan import TUNING_END, History

PREREGISTRATION_COMMIT = "719da60"
"""The commit that holds the approved pre-registration (task 3.9)."""
FROZEN_HASH: str | None = None
"""`risk.yaml`'s hash once 3.16 freezes it; the holdout refuses to run before then."""
CHOSEN = "cu+2_corr-2_c2_dist8"
"""The 5-pillar regime variant the overlay reads (`docs/backtests/regime-5-pillar.md`)."""
TUNING_START = date(2017, 1, 31)
REPORT = REPO_ROOT / "docs" / "backtests" / "momentum.md"
BLIND_REPORT = REPO_ROOT / "docs" / "backtests" / "momentum-blind.md"
HOLDOUT_REPORT = REPO_ROOT / "docs" / "backtests" / "momentum-holdout.md"
DETAIL = REPO_ROOT / "var" / "backtests" / "momentum"
WHOLE_UNIVERSE = 1_000_000
"""C6's `universe_size`: no liquidity cut."""


def _log(message: str) -> None:
    print(message, file=sys.stderr)


# --- the market --------------------------------------------------------------------------


def book_series(key: str, symbol: str, s: SecuritySeries) -> BookSeries:
    return BookSeries(key, symbol, s.dates, s.open, s.high, s.low, s.close, s.raw_close, s.volume)


def successors(symbols: Mapping[str, str], firsts: Mapping[str, date]) -> dict[str, str]:
    """Each security -> the next piece of the same ticker (by first session), among those
    loaded."""
    pieces: dict[str, list[tuple[date, str]]] = defaultdict(list)
    for key, first in firsts.items():
        pieces[symbols[key]].append((first, key))
    out = {}
    for listed in pieces.values():
        listed.sort()
        for (_, a), (_, b) in pairwise(listed):
            out[a] = b
    return out


def above_sma(dates: Sequence[date], closes: Sequence[float], n: int) -> dict[date, bool]:
    """Each session's close against the mean of its last ``n`` closes; sessions without ``n``
    closes are left out (C4 then blocks: fail closed)."""
    out = {}
    total = 0.0
    for i, c in enumerate(closes):
        total += c
        if i >= n:
            total -= closes[i - n]
        if i >= n - 1:
            out[dates[i]] = c > total / n
    return out


def reactions(reports: Sequence[tuple[date, EarningsTiming]], days: Sequence[date]) -> list[date]:
    """The calendar sessions that react to each report (README §5.1's timing): the report
    session, or the next one after the close; a report on a day without a session reacts on
    the next session."""
    out = set()
    for day, timing in reports:
        i = bisect.bisect_left(days, day)
        on_session = i < len(days) and days[i] == day
        j = reaction_session(i, timing) if on_session else i
        if j < len(days):
            out.add(days[j])
    return sorted(out)


def book_market(
    h: History, days: Sequence[Session], results: Sequence[RegimeResult], risk: RiskConfig
) -> BookMarket:
    calendar = [s.day for s in days]
    spy = h.series[h.indexes["SPY"]]
    return BookMarket(
        sessions=days,
        series={k: book_series(k, h.symbol(k), s) for k, s in h.series.items() if len(s)},
        members=h.members,
        regimes={r.session: r.regime for r in results},
        crisis=frozenset(r.session for r in results if r.crisis.active),
        industries=h.groups,
        successors=successors(
            {k: h.symbol(k) for k in h.series},
            {k: s.dates[0] for k, s in h.series.items() if len(s)},
        ),
        spy_above_sma=above_sma(spy.dates, spy.close, risk.book.overlay_sma_sessions),
        reactions={k: reactions(v, calendar) for k, v in h.earnings.items()},
    )


# --- configurations ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Config:
    name: str
    change: str
    risk: RiskConfig
    kind: mr.Kind | None = None
    equity: float | None = None
    """None: the backtest's starting equity."""
    offset: int = 1


def with_book(risk: RiskConfig, **update: Any) -> RiskConfig:
    return risk.model_copy(update={"book": risk.book.model_copy(update=update)})


COMPARISONS: dict[str, tuple[str, mr.Kind, dict[str, Any]]] = {
    "C1": ("signal: nearness to the 52-week high", mr.Kind.CHANGE, {"signal": BookSignal.HIGH_52W}),
    "C2": ("stop at 10%", mr.Kind.CHANGE, {"stop_fraction": 0.10}),
    "C3": ("overlay off", mr.Kind.REMOVE_SAFETY, {"overlay": BookOverlay.OFF}),
    "C4": (
        "overlay = SPY above its 200-session SMA",
        mr.Kind.CHANGE,
        {"overlay": BookOverlay.SPY_SMA},
    ),
    "C5": ("earnings rules on", mr.Kind.ADD_SAFETY, {"earnings_rules": True}),
    "C6": ("the whole universe", mr.Kind.CHANGE, {"universe_size": WHOLE_UNIVERSE}),
    "C7": ("cluster cap off", mr.Kind.REMOVE_SAFETY, {"cluster_cap": False}),
}


def comparisons(risk: RiskConfig) -> list[Config]:
    return [
        Config(name, change, with_book(risk, **update), kind)
        for name, (change, kind, update) in COMPARISONS.items()
    ]


def adopted_config(risk: RiskConfig, names: Sequence[str]) -> Config:
    update: dict[str, Any] = {}
    for name in names:
        update |= COMPARISONS[name][2]
    return Config("adopted", " + ".join(names), with_book(risk, **update))


def zero_cost(risk: RiskConfig) -> RiskConfig:
    tiers = tuple(t.model_copy(update={"fraction": 0.0}) for t in risk.sim.slippage)
    return risk.model_copy(update={"sim": risk.sim.model_copy(update={"slippage": tiers})})


def diagnostics(risk: RiskConfig) -> list[Config]:
    bt = risk.book.backtest
    out = [
        Config(f"offset {n}", f"rebalance on session {n} of the month", risk, offset=n)
        for n in bt.timing_offsets
        if n != 1
    ]
    out.append(Config("notional", f"${bt.notional_equity:,.0f}", risk, equity=bt.notional_equity))
    out.append(Config("zero cost", "no slippage", zero_cost(risk)))
    return out


# --- runs --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Window:
    start: date
    """The cash start: the book is marked from this close."""
    end: date


@dataclass(frozen=True, slots=True)
class Inputs:
    market: BookMarket
    spy: Mapping[date, float]
    mtum: Mapping[date, float]
    """Total-return closes."""


@dataclass(frozen=True, slots=True)
class Result:
    config: Config
    run: BookRun

    @property
    def curve(self) -> mr.Curve:
        return mr.of_run(self.run)


def run_config(inputs: Inputs, config: Config, window: Window) -> Result:
    equity = config.equity or config.risk.book.backtest.start_equity
    run = simulate_book(
        inputs.market,
        config.risk,
        start=window.start,
        end=window.end,
        equity=equity,
        offset=config.offset,
    )
    return Result(config, run)


def reference_periods(inputs: Inputs, risk: RiskConfig, window: Window) -> list[mr.Period]:
    """The ranked set of each E in the window, with each name's signal and its price relative
    to the next E (or the window's end)."""
    market = inputs.market
    due = [r for r in rebalances(market.sessions) if window.start < r.execution <= window.end]
    ends = [r.execution for r in due[1:]] + [window.end]
    out = []
    for r, until in zip(due, ends, strict=True):
        names = {}
        for x in ranking_on(market, risk, r.ranking):
            s = market.series[x.key]
            a = bisect.bisect_right(s.dates, r.execution) - 1
            b = bisect.bisect_right(s.dates, until) - 1
            if a >= 0 and s.close[a] > 0:
                names[x.key] = mr.Holding(x.signal, s.close[b] / s.close[a])
        out.append(mr.Period(r.execution, until, names))
    return out


@dataclass(frozen=True, slots=True)
class Scored:
    window: Window
    risk: RiskConfig
    primary: Result
    compared: list[tuple[Result, mr.Judgement]]
    adopted: Result | None
    diagnostics: list[Result]
    spy: mr.Curve
    mtum: mr.Curve
    equal_weight: mr.Curve
    signal: mr.SignalCheck
    position: Mapping[date, int]
    next_session: Mapping[date, date]
    regimes: Mapping[date, Regime]

    @property
    def adopted_names(self) -> list[str]:
        return mr.adopted([j for _, j in self.compared])

    def gate_of(self, result: Result) -> mr.Gate:
        return mr.gate(result.curve, self.spy, self.mtum, self.risk.book.backtest)

    @property
    def frozen(self) -> Result | None:
        """The configuration that passes the gate and would be frozen: the adopted one, else
        the primary; None on a fail."""
        if self.adopted is not None and self.gate_of(self.adopted).passed:
            return self.adopted
        return self.primary if self.gate_of(self.primary).passed else None


def _calendar_maps(market: BookMarket) -> tuple[dict[date, int], dict[date, date]]:
    days = [s.day for s in market.sessions]
    return {d: i for i, d in enumerate(days)}, dict(pairwise(days))


def scored(
    inputs: Inputs, risk: RiskConfig, window: Window, log: Callable[[str], None] = _log
) -> Scored:
    """The scored run (§2 steps 4-5): the primary, C1-C7, the adopted configuration and the
    diagnostics, on the tuning period."""
    clock = time.monotonic()
    primary = run_config(inputs, Config("primary", "as pre-registered", risk), window)
    log(f"primary: {time.monotonic() - clock:.0f} s")
    bt = risk.book.backtest
    compared = []
    for config in comparisons(risk):
        result = run_config(inputs, config, window)
        kind = config.kind or mr.Kind.CHANGE
        compared.append((result, mr.judge(config.name, kind, result.curve, primary.curve, bt)))
        log(f"{config.name}: {time.monotonic() - clock:.0f} s")
    names = mr.adopted([j for _, j in compared])
    adopted = run_config(inputs, adopted_config(risk, names), window) if names else None
    extra = [run_config(inputs, c, window) for c in diagnostics(risk)]
    log(f"diagnostics: {time.monotonic() - clock:.0f} s")
    dates = primary.curve.dates
    periods = reference_periods(inputs, risk, window)
    position, next_session = _calendar_maps(inputs.market)
    return Scored(
        window=window,
        risk=risk,
        primary=primary,
        compared=compared,
        adopted=adopted,
        diagnostics=extra,
        spy=mr.aligned(inputs.spy, dates),
        mtum=mr.aligned(inputs.mtum, dates),
        equal_weight=mr.equal_weight(periods, window.start, bt.start_equity),
        signal=mr.signal_check(periods),
        position=position,
        next_session=next_session,
        regimes=inputs.market.regimes,
    )


@dataclass(frozen=True, slots=True)
class Blind:
    window: Window
    risk: RiskConfig
    counts: dict[int | None, mr.BlindCounts]
    checked: list[str]
    """The configurations run with every invariant holding."""

    @property
    def stops(self) -> list[str]:
        return mr.stop_conditions(self.counts[None], self.risk.book.backtest)


def blind(
    inputs: Inputs, risk: RiskConfig, window: Window, log: Callable[[str], None] = _log
) -> Blind:
    """The blind mechanics check (§2 step 3, §7): the primary's counts; C1-C7 run for their
    invariants only. Nothing about equity or returns leaves this function."""
    primary = run_config(inputs, Config("primary", "as pre-registered", risk), window)
    checked = ["primary"]
    for config in comparisons(risk):
        run_config(inputs, config, window)
        checked.append(config.name)
        log(f"{config.name}: invariants held")
    position, _ = _calendar_maps(inputs.market)
    return Blind(window, risk, mr.blind_counts(primary.run, position), checked)


@dataclass(frozen=True, slots=True)
class Holdout:
    window: Window
    risk: RiskConfig
    result: Result
    spy: mr.Curve
    mtum: mr.Curve

    @property
    def h1(self) -> bool:
        return mr.performance(self.result.curve).cagr > mr.performance(self.spy).cagr

    @property
    def h2(self) -> bool:
        return mr.performance(self.result.curve).cagr >= mr.performance(self.mtum).cagr


def check_frozen(risk: RiskConfig, frozen: str | None) -> None:
    """The holdout opens only for the configuration frozen in 3.16."""
    if frozen is None or risk.config_hash != frozen:
        raise SystemExit(
            "the holdout opens only for the frozen configuration: risk.yaml must hash to "
            f"FROZEN_HASH ({frozen}), not {risk.config_hash}"
        )


def holdout(inputs: Inputs, risk: RiskConfig, window: Window, frozen: str | None) -> Holdout:
    """The frozen configuration over the holdout, once (§2 step 6)."""
    check_frozen(risk, frozen)
    result = run_config(inputs, Config("frozen", "the frozen configuration", risk), window)
    dates = result.curve.dates
    return Holdout(
        window, risk, result, mr.aligned(inputs.spy, dates), mr.aligned(inputs.mtum, dates)
    )


# --- loading -----------------------------------------------------------------------------


def _regime_results(h: History, risk: RiskConfig, last: date) -> list[RegimeResult]:
    """The `CHOSEN` variant's 5-pillar regime from 2016 (cached: it reads no book result)."""
    from auratrade.config import Strategy
    from auratrade.strategies.registry import build_strategies
    from backtest.baseline import variant_named
    from backtest.history_scan import scan_pass, track
    from backtest.regime_search import fixed_points, regime_inputs

    cache = DETAIL / f"regime-{CHOSEN}-{last.isoformat()}-{risk.config_hash[:12]}.pickle"
    if cache.exists():
        with cache.open("rb") as f:
            cached: list[RegimeResult] = pickle.load(f)  # noqa: S301 - our own cache
            return cached
    strategies = build_strategies(risk)
    core = [s for k, s in strategies.items() if k is not Strategy.EMERGING_LEADER]
    (found,) = scan_pass(h, risk, core, [()], features=False)
    variant = variant_named(CHOSEN)
    converged = fixed_points(
        h,
        risk,
        regime_inputs(h, risk),
        [variant],
        track(h, found, risk),
        strategies[Strategy.EMERGING_LEADER],
        macro_stale_limit=risk.data.macro_stale_sessions,
        log=_log,
    )[variant]
    DETAIL.mkdir(parents=True, exist_ok=True)
    with cache.open("wb") as f:
        pickle.dump(converged.results, f)
    return converged.results


def load_inputs(risk: RiskConfig, *, last: date | None) -> tuple[Inputs, list[Session]]:
    """The market and benchmarks through ``last`` (the tuning period's end; None: the latest
    backfilled session, for the holdout only)."""
    from auratrade.calendar.sessions import load_calendar
    from auratrade.db.engine import engine_from_settings
    from auratrade.history.benchmarks import load_benchmarks
    from auratrade.history.store import bar_sessions
    from auratrade.jobs.wiring import load_runtime_settings
    from auratrade.storage import make_store
    from backtest.history_scan import load_history

    settings = load_runtime_settings()
    engine = engine_from_settings(settings)
    try:
        with engine.connect() as conn:
            calendar = load_calendar(conn)
    finally:
        engine.dispose()
    if calendar.first is None:
        raise RuntimeError("the market calendar is empty")
    store = make_store(settings)
    through = last if last is not None else bar_sessions(store)[-1]
    days = calendar.sessions_between(calendar.first, through)
    h = load_history(store, days, risk, last=through, log=_log)
    loaded = set(h.sessions)
    days = [s for s in days if s.day in loaded]
    market = book_market(h, days, _regime_results(h, risk, through), risk)
    bench = load_benchmarks(store)
    closes = {
        symbol: {d: c for d, c in zip(b.dates, b.closes, strict=True) if d <= through}
        for symbol, b in bench.items()
    }
    return Inputs(market, closes["SPY"], closes["MTUM"]), days


def main(argv: Sequence[str] | None = None) -> int:
    from auratrade.jobs.wiring import load_runtime_settings
    from backtest.momentum_render import render_blind, render_holdout, render_scored

    parser = argparse.ArgumentParser(prog="python -m backtest.momentum")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--blind", action="store_true", help="3.14: counts and invariants only")
    mode.add_argument("--holdout", action="store_true", help="3.16: the frozen configuration")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    clock = time.monotonic()
    risk = load_risk_config(load_runtime_settings().risk_config_path)
    if args.holdout:
        check_frozen(risk, FROZEN_HASH)  # before anything after TUNING_END is loaded
    inputs, days = load_inputs(risk, last=None if args.holdout else TUNING_END)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    if args.holdout:
        window = Window(TUNING_END, days[-1].day)
        text = render_holdout(holdout(inputs, risk, window, FROZEN_HASH), stamp)
        report = args.report or HOLDOUT_REPORT
    elif args.blind:
        text = render_blind(blind(inputs, risk, Window(TUNING_START, TUNING_END)), stamp)
        report = args.report or BLIND_REPORT
    else:
        result = scored(inputs, risk, Window(TUNING_START, TUNING_END))
        text = render_scored(result, stamp)
        report = args.report or REPORT
        DETAIL.mkdir(parents=True, exist_ok=True)
        runs = [result.primary, *(r for r, _ in result.compared), *result.diagnostics]
        if result.adopted is not None:
            runs.append(result.adopted)
        with (DETAIL / "scored.pickle").open("wb") as f:
            pickle.dump({r.config.name: r.run for r in runs}, f)
    report.write_text(
        text + f"\n_Run time: {time.monotonic() - clock:.0f} s._\n", encoding="utf-8", newline="\n"
    )
    _log(f"wrote {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

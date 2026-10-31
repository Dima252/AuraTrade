"""The portfolio manager's backtest (tasks 3.21-3.23;
`docs/backtests/portfolio-preregistration.md`).

    uv run python -m backtest.portfolio --blind   # 3.22: counts, invariants, data checks
    uv run python -m backtest.portfolio           # 3.23: the one scored run, with the user

Everything is fixed in the pre-registration, approved and committed (`PREREGISTRATION_COMMIT`)
before any of this read data. **One scored run:** the scored report is refused if it already
exists; a second run would be a new trial, which the pre-registration doesn't allow (§9).

- **The market:** Kenneth French's daily market and T-bill returns and Shiller's dividend yield
  (carried forward past its last month, #9), synthesized into the three funds (§3.2).
- **The runs:** the primary (the twin's rules) and the benchmark, each after tax and before;
  the rolling 10-year windows, each an independent run (§3.1); the periods after publication;
  C1-C5 and D-a to D-c, reported and never adopted (§6).
- **The data checks:** the market fund against SPY's total return and the 2x fund against
  SSO's, 2016 on (§4.4).
"""

from __future__ import annotations

import argparse
import sys
import time
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from auratrade.config import REPO_ROOT, PortfolioConfig, RiskConfig, load_risk_config
from auratrade.history.benchmarks import PORTFOLIO_FUNDS, Benchmark, load_benchmarks
from auratrade.history.long_history import LongHistory, load_long_history
from auratrade.sim.portfolio import (
    BENCHMARK,
    LeverageMode,
    Market,
    PortfolioRun,
    Rules,
    TrendRule,
    simulate_portfolio,
    synthesize,
)
from backtest import portfolio_report as pr

PREREGISTRATION_COMMIT = "939292f"
REPORT = REPO_ROOT / "docs" / "backtests" / "portfolio.md"
BLIND_REPORT = REPO_ROOT / "docs" / "backtests" / "portfolio-blind.md"
PUBLISHED = (("Faber (2007)", date(2007, 1, 1)), ("Gayed & Bilello (2016)", date(2016, 1, 1)))
CHECKS_FROM = date(2016, 1, 1)


def _log(message: str) -> None:
    print(f"[portfolio {datetime.now(UTC):%H:%M:%S}] {message}", file=sys.stderr, flush=True)


# --- the market ------------------------------------------------------------------------------


def dividend_yields(history: LongHistory) -> list[float]:
    """Each session's annual dividend yield (D / P) from its month's Shiller row; months after
    the last published dividend carry the last yield forward (#9)."""
    by_month: dict[tuple[int, int], float] = {}
    last: float | None = None
    for month, price, dividend in zip(
        history.months, history.prices, history.dividends, strict=True
    ):
        if dividend is not None:
            last = dividend / price
        if last is not None:
            by_month[(month.year, month.month)] = last
    out = []
    for day in history.days:
        key = (day.year, day.month)
        if key not in by_month:
            raise ValueError(f"no dividend yield for {day:%Y-%m}")
        out.append(by_month[key])
    return out


def build_market(history: LongHistory, cfg: PortfolioConfig) -> Market:
    return synthesize(history.days, history.market, history.rf, dividend_yields(history), cfg)


def month_ends(days: Sequence[date]) -> list[int]:
    """The last session of each month (R in the pre-registration's definition, §3.1)."""
    return [i for i in range(len(days)) if i + 1 == len(days) or days[i + 1].month != days[i].month]


@dataclass(frozen=True, slots=True)
class Period:
    start: int
    """The session index of the first R (cash at its close)."""
    end: int
    """The last R: everything is sold at its close."""


def scored_period(days: Sequence[date], cfg: PortfolioConfig) -> Period:
    ends = month_ends(days)
    first = next(
        i for i in ends if days[i].year == cfg.backtest.score_start_year - 1 and days[i].month == 12
    )
    return Period(first, ends[-1])


def windows(days: Sequence[date], period: Period, cfg: PortfolioConfig) -> list[Period]:
    """Every 10-year window: a start at each R from the first through the one 120 months
    before the end."""
    ends = [i for i in month_ends(days) if period.start <= i <= period.end]
    span = cfg.backtest.window_years * 12
    return [Period(ends[k], ends[k + span]) for k in range(len(ends) - span)]


# --- the runs --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Config:
    name: str
    change: str
    rules: Rules
    cfg: PortfolioConfig
    offset: int = 1


def configurations(cfg: PortfolioConfig) -> tuple[Config, list[Config], list[Config]]:
    """The primary, the comparisons C1-C5 and the diagnostics D-a to D-c (§6)."""
    bt = cfg.backtest
    primary = Config("primary", "the pre-registered rules", Rules(), cfg)
    c2 = cfg.model_copy(update={"up_level": bt.c2_up_level, "down_level": bt.c2_down_level})
    compared = [
        Config(
            "C1",
            f"the trend from the {bt.c1_sma_sessions}-session SMA",
            Rules(trend=TrendRule.DAILY_SMA, sma_sessions=bt.c1_sma_sessions),
            cfg,
        ),
        Config("C2", f"{bt.c2_up_level}x UP, {bt.c2_down_level}x DOWN", Rules(), c2),
        Config("C3", "leverage by margin", Rules(leverage=LeverageMode.MARGIN), cfg),
        Config("C4", "no catastrophe stop", Rules(stop=False), cfg),
        Config("C5", "no volatility cap", Rules(vol_cap=False), cfg),
    ]
    trend_alone = cfg.model_copy(update={"up_level": 1.0, "down_level": 0.0})
    diagnostics = [
        Config("D-a", "the trend alone: 1.0x UP, 0x DOWN", Rules(), trend_alone),
        Config(
            "D-b",
            f"leverage alone: a constant {cfg.up_level}x",
            Rules(trend=TrendRule.NONE, constant_level=cfg.up_level, vol_cap=False),
            cfg,
        ),
        *(
            Config(f"D-c{k}", f"E on session {k} of the month", Rules(), cfg, offset=k)
            for k in cfg.backtest.timing_offsets
            if k != 1
        ),
    ]
    return primary, compared, diagnostics


def simulate(
    market: Market, config: Config, period: Period, tax_rate: float, equity: float
) -> PortfolioRun:
    return simulate_portfolio(
        market,
        config.rules,
        config.cfg,
        start=period.start,
        end=period.end,
        equity=equity,
        tax_rate=tax_rate,
        offset=config.offset,
    )


@dataclass(frozen=True, slots=True)
class Pair:
    after: PortfolioRun
    before: PortfolioRun

    @property
    def metrics(self) -> pr.Metrics:
        return pr.metrics(self.after, self.before)


def pair(market: Market, config: Config, period: Period, risk: RiskConfig) -> Pair:
    equity = risk.portfolio.backtest.start_equity
    rate = risk.portfolio.tax_rate
    return Pair(
        simulate(market, config, period, rate, equity),
        simulate(market, config, period, 0.0, equity),
    )


def window_results(
    market: Market, primary: Config, benchmark: Config, spans: Sequence[Period], risk: RiskConfig
) -> list[pr.Window]:
    out = []
    equity, rate = risk.portfolio.backtest.start_equity, risk.portfolio.tax_rate
    for k, span in enumerate(spans):
        if k % 120 == 0:
            _log(f"window {k + 1} of {len(spans)}")
        strategy = simulate(market, primary, span, rate, equity)
        held = simulate(market, benchmark, span, rate, equity)
        out.append(
            pr.Window(
                market.days[span.start],
                market.days[span.end],
                strategy.final_wealth,
                held.final_wealth,
            )
        )
    return out


# --- the data checks -------------------------------------------------------------------------


def aligned(
    days: Sequence[date], series: Sequence[float], real: Benchmark, since: date
) -> tuple[list[float], list[float]]:
    """The proxy and the real fund on the sessions they share from ``since``."""
    at = {d: i for i, d in enumerate(days)}
    proxy, fund = [], []
    for day, close in zip(real.dates, real.closes, strict=True):
        if day >= since and day in at:
            proxy.append(series[at[day]])
            fund.append(close)
    return proxy, fund


def data_checks(
    market: Market, funds: dict[str, Benchmark], cfg: PortfolioConfig, since: date = CHECKS_FROM
) -> list[pr.DataCheck]:
    out = []
    for name, series, symbol in (
        ("the market fund against SPY", market.index, cfg.market),
        ("the 2x fund against SSO", market.prices[cfg.leveraged], cfg.leveraged),
    ):
        proxy, real = aligned(market.days, series, funds[symbol], since)
        sessions = set(market.days)
        shared = [d for d in funds[symbol].dates if d >= since and d in sessions]
        if len(shared) < 2:
            raise ValueError(f"{symbol}: no sessions shared with the market file from {since}")
        years = pr.years_between(shared[0], shared[-1])
        out.append(pr.data_check(name, proxy, real, years, cfg.backtest))
    return out


# --- the two modes ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Blind:
    history: LongHistory
    period: Period
    window_count: int
    counts: pr.BlindCounts
    checks: list[pr.DataCheck]
    gaps: list[tuple[date, date, bool]]
    stops: list[str]
    sessions_by_decade: dict[str, int]
    flips_by_decade: dict[str, int]


def blind(
    history: LongHistory,
    funds: dict[str, Benchmark],
    risk: RiskConfig,
    checks_from: date = CHECKS_FROM,
) -> Blind:
    cfg = risk.portfolio
    market = build_market(history, cfg)
    period = scored_period(market.days, cfg)
    primary, _, _ = configurations(cfg)
    run = simulate(market, primary, period, cfg.tax_rate, cfg.backtest.start_equity)
    counts = pr.blind_counts(run)
    checks = data_checks(market, funds, cfg, checks_from)
    gaps = pr.session_gaps(market.days[period.start : period.end + 1], cfg.backtest)
    sessions: dict[str, int] = {}
    for day in market.days[period.start : period.end + 1]:
        key = f"{day.year - day.year % 10}s"
        sessions[key] = sessions.get(key, 0) + 1
    flips: dict[str, int] = {}
    for r in run.rankings:
        key = f"{r.day.year - r.day.year % 10}s"
        flips[key] = flips.get(key, 0) + r.flipped
    return Blind(
        history,
        period,
        len(windows(market.days, period, cfg)),
        counts,
        checks,
        gaps,
        pr.blind_stops(counts, checks, gaps, cfg.backtest),
        sessions,
        flips,
    )


@dataclass(frozen=True, slots=True)
class Scored:
    history: LongHistory
    period: Period
    days: tuple[date, date]
    primary: Pair
    benchmark: Pair
    windows: list[pr.Window]
    gate: pr.Gate
    published: list[tuple[str, date, pr.Metrics, pr.Metrics]]
    compared: list[tuple[Config, pr.Metrics]]
    diagnostics: list[tuple[Config, pr.Metrics]]
    crashes: list[pr.Crash]
    decades: list[tuple[str, float, float]]
    bootstrap: pr.Bootstrap


def scored(history: LongHistory, risk: RiskConfig) -> Scored:
    cfg = risk.portfolio
    market = build_market(history, cfg)
    period = scored_period(market.days, cfg)
    primary, compared, diagnostics = configurations(cfg)
    held = Config("benchmark", "the market fund bought, held and sold at the end", BENCHMARK, cfg)
    _log("the primary and the benchmark")
    main, bench = pair(market, primary, period, risk), pair(market, held, period, risk)
    spans = windows(market.days, period, cfg)
    results = window_results(market, primary, held, spans, risk)
    verdict = pr.gate(main.metrics, bench.metrics, results, cfg.backtest)
    after = []
    ends = month_ends(market.days)
    for name, since in PUBLISHED:
        if since >= market.days[period.end]:
            continue
        first = ends[bisect_left([market.days[i] for i in ends], since) - 1]
        span = Period(first, period.end)
        after.append(
            (
                name,
                since,
                pair(market, primary, span, risk).metrics,
                pair(market, held, span, risk).metrics,
            )
        )
    _log("the comparisons and the diagnostics")
    return Scored(
        history,
        period,
        (market.days[period.start], market.days[period.end]),
        main,
        bench,
        results,
        verdict,
        after,
        [(c, pair(market, c, period, risk).metrics) for c in compared],
        [(c, pair(market, c, period, risk).metrics) for c in diagnostics],
        pr.crashes(main.after, bench.after),
        pr.decades(main.before, bench.before),
        pr.block_bootstrap(main.before.marks, bench.before.marks, cfg.backtest),
    )


def main(argv: Sequence[str] | None = None) -> int:
    from auratrade.jobs.wiring import load_runtime_settings
    from auratrade.storage import make_store
    from backtest.portfolio_render import render_blind, render_scored

    parser = argparse.ArgumentParser(prog="python -m backtest.portfolio")
    parser.add_argument("--blind", action="store_true", help="3.22: counts and checks only")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    report = args.report or (BLIND_REPORT if args.blind else REPORT)
    if not args.blind and args.report is None and REPORT.exists():
        _log(f"{REPORT} exists: the scored run is one-shot (pre-registration §9)")
        return 2
    clock = time.monotonic()
    settings = load_runtime_settings()
    risk = load_risk_config(settings.risk_config_path)
    store = make_store(settings)
    history = load_long_history(store)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    if args.blind:
        funds = load_benchmarks(store, PORTFOLIO_FUNDS)
        text = render_blind(blind(history, funds, risk, CHECKS_FROM), risk, stamp)
    else:
        text = render_scored(scored(history, risk), risk, stamp)
    report.write_text(
        text + f"\n_Run time: {time.monotonic() - clock:.0f} s._\n", encoding="utf-8", newline="\n"
    )
    _log(f"wrote {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

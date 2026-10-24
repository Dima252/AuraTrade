"""The baseline backtest (task 3.8; README §15): the historical full scan, the breakout tracker,
the 5-pillar regime re-run, then the setup pool and the baseline book.

    uv run python -m backtest.baseline regime    # steps 1-3 -> docs/backtests/regime-5-pillar.md
    uv run python -m backtest.baseline pool      # step 4: the pool and gate -> baseline.md
    uv run python -m backtest.baseline pool --count   # only count the minute bars it would fetch

Everything is fixed in `docs/backtests/baseline-preregistration.md`, written before any of this
ran on real data. **The holdout stays closed:** nothing after `TUNING_END` is loaded until the
chosen regime and defaults are frozen in `risk.yaml` (step 5).
"""

from __future__ import annotations

import argparse
import pickle
import statistics
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from auratrade.calendar.sessions import Session
from auratrade.config import REPO_ROOT, Regime, RiskConfig, Settings, Strategy, load_risk_config
from auratrade.sim.minutes import MinuteCache
from auratrade.sim.sessions import Minutes
from auratrade.storage import ObjectStore
from auratrade.strategies.registry import build_strategies
from auratrade.strategies.watchlist import correction_spells
from backtest import regime as bt
from backtest.history_scan import TUNING_END, History, TrackerRow, load_history, scan_pass, track
from backtest.pool import (
    Listing,
    PoolInputs,
    PoolTrade,
    capped_lists,
    prefetch,
    run_pool,
    traded_symbols,
)
from backtest.pool_report import render_pool
from backtest.regime_search import (
    VARIANTS,
    Converged,
    Variant,
    choose,
    fixed_points,
    regime_inputs,
    regimes_of,
)

REGIME_REPORT = REPO_ROOT / "docs" / "backtests" / "regime-5-pillar.md"
POOL_REPORT = REPO_ROOT / "docs" / "backtests" / "baseline.md"
DETAIL = REPO_ROOT / "var" / "backtests" / "baseline"
CHOSEN = "cu+2_corr-2_c2_dist8"
"""The variant step 3 chose (docs/backtests/regime-5-pillar.md)."""


def _log(message: str) -> None:
    print(message, file=sys.stderr)


def spy_of(h: History) -> bt.Spy:
    s = h.series[h.indexes["SPY"]]
    return bt.Spy(s.dates, list(s.close))


def run_regime(
    h: History, risk: RiskConfig, log: Callable[[str], None] = _log
) -> tuple[dict[Variant, Converged], dict[Variant, bt.Summary], Variant, list[TrackerRow]]:
    """Steps 1-3 on ``h``: the core scan's tracker rows, each variant's fixed point, the
    summaries against 2.4's targets and the chosen variant."""
    clock = time.monotonic()
    strategies = build_strategies(risk)
    core = [s for k, s in strategies.items() if k is not Strategy.EMERGING_LEADER]
    (found,) = scan_pass(h, risk, core, [()], features=False)
    core_rows = track(h, found, risk)
    log(f"core scan and tracker: {time.monotonic() - clock:.0f} s ({len(core_rows)} triggers)")
    inputs = regime_inputs(h, risk)
    log(f"regime inputs: {time.monotonic() - clock:.0f} s")
    converged = fixed_points(
        h,
        risk,
        inputs,
        VARIANTS,
        core_rows,
        strategies[Strategy.EMERGING_LEADER],
        macro_stale_limit=risk.data.macro_stale_sessions,
        log=log,
    )
    spy = spy_of(h)
    summaries = {
        v: bt.summarize(v.name, v.description, c.results, spy) for v, c in converged.items()
    }
    return converged, summaries, choose(summaries), core_rows


# --- The regime report --------------------------------------------------------------------------


def _tracker_lines(rows: Sequence[TrackerRow]) -> list[str]:
    years = sorted({r.session.year for r in rows if r.session >= bt.FIRST_REPORTED})
    lines = [
        "| Year | Triggers | Per session (median) | Above the pivot 5 sessions later |",
        "|---|---|---|---|",
    ]
    for year in years:
        shown = [r for r in rows if r.session.year == year]
        per = Counter(r.session for r in shown)
        done = [r.above_trigger for r in shown if r.above_trigger is not None]
        lines.append(
            f"| {year} | {len(shown):,} | {statistics.median(per.values()):.0f} | "
            f"{bt._pct(sum(done) / len(done) if done else None, 0)} |"
        )
    by = Counter(r.strategy for r in rows)
    lines += ["", "By strategy: " + ", ".join(f"`{s.value}` {n:,}" for s, n in by.items()) + "."]
    return lines


def render_regime(
    h: History,
    risk: RiskConfig,
    converged: Mapping[Variant, Converged],
    summaries: Mapping[Variant, bt.Summary],
    chosen: Variant,
    seconds: float,
    reading: str = "",
) -> str:
    spy = spy_of(h)
    c = converged[chosen]
    four = bt.summarize(
        f"{chosen.name}_4_pillars",
        f"{chosen.description}; leadership at 0 (4 pillars)",
        c.four_pillar,
        spy,
    )
    ordered = sorted(summaries.values(), key=lambda s: s.name)
    counts = bt.pillar_counts(bt.reported(c.results))["leadership"]
    shown = sum(counts.values())
    lines = [
        "# Regime re-run on 5 pillars (task 3.8, step 3)",
        "",
        f"**Run:** {datetime.now(UTC):%Y-%m-%d %H:%M} UTC with `uv run python "
        f"-m backtest.baseline regime`, risk.yaml `{risk.short_hash}`, {seconds:.0f} s. Generated; "
        "only the Reading at the end is written by hand. The 4-pillar run of task 2.4 is in "
        "[regime.md](regime.md).",
        "",
        "## Setup",
        "",
        f"- **The tuning period only:** sessions {h.sessions[0]} to {h.sessions[-1]}. Nothing "
        f"after {TUNING_END} is loaded; the holdout is opened once, after the thresholds are "
        "frozen (pre-registration step 5).",
        "- **Leadership health** (README §5.2) from the breakout tracker over the historical full "
        "scan (every strategy on every point-in-time member from 2017; `backtest/history_scan.py`"
        "), by the live rules (#142). It scores from 10 complete breakouts in its window, so it "
        "starts in February 2017. The score runs from -5 to +5.",
        "- **The loop:** the `EmergingLeader` scan reads the regime's `CORRECTION` spells, so each "
        "variant is iterated to its fixed point (`backtest/regime_search.py`). Iterations: "
        + ", ".join(
            f"{n} variant(s) in {k}"
            for k, n in sorted(Counter(x.iterations for x in converged.values()).items())
        )
        + ".",
        "- **Pre-registered** (docs/backtests/baseline-preregistration.md): the 16 variants and "
        "2.4's five targets; the choice is the variant passing the most, ties to the fewest "
        "switches a year, then the closest to IBD's values (+3, <= -1, 6 days). Moving the "
        "`CONFIRMED_UPTREND` threshold also moves the CU -> UP debounce (one below it) and the "
        "`no_ftd` fallback (at it), as 2.4's variants did.",
        "",
        "## The tracker",
        "",
        *_tracker_lines(c.rows),
        "",
        f"Leadership pillar over the reported sessions ({chosen.name}): "
        + ", ".join(f"{score:+d} {bt._pct(n / shown, 0)}" for score, n in sorted(counts.items()))
        + ".",
        "",
        "## The search",
        "",
        *bt.render_comparisons([*ordered, four]),
        "",
        f"**Chosen: `{chosen.name}`** ({chosen.description}): "
        f"{sum(summaries[chosen].checks().values())} of 5 targets, "
        f"{summaries[chosen].mean_switches:.1f} switches a year.",
        "",
        "## The chosen variant",
        "",
        *bt.render_detail(chosen.name, c.results, spy, summaries[chosen]),
        "",
        reading.rstrip() if reading else f"{bt.READING}\n\n(Not written yet.)",
        "",
    ]
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class Loaded:
    h: History
    calendar: list[Session]
    store: ObjectStore
    settings: Settings


def _load(risk: RiskConfig) -> Loaded:
    from auratrade.calendar.sessions import load_calendar
    from auratrade.db.engine import engine_from_settings
    from auratrade.jobs.wiring import load_runtime_settings
    from auratrade.storage import make_store

    settings = load_runtime_settings()
    engine = engine_from_settings(settings)
    try:
        with engine.connect() as conn:
            calendar = load_calendar(conn)
    finally:
        engine.dispose()
    if calendar.first is None:
        raise RuntimeError("the market calendar is empty")
    days = calendar.sessions_between(calendar.first, TUNING_END)
    store = make_store(settings)
    h = load_history(store, days, risk, last=TUNING_END, log=_log)
    return Loaded(h, days, store, settings)


def variant_named(name: str) -> Variant:
    for v in VARIANTS:
        if v.name == name:
            return v
    raise SystemExit(f"no variant {name!r}; one of {', '.join(v.name for v in VARIANTS)}")


def pool_inputs(
    loaded: Loaded, risk: RiskConfig, variant: Variant, log: Callable[[str], None] = _log
) -> tuple[PoolInputs, dict[date, list[Listing]]]:
    """The chosen variant's fixed point, the full scan with features and the capped lists."""
    h = loaded.h
    clock = time.monotonic()
    strategies = build_strategies(risk)
    core = [s for k, s in strategies.items() if k is not Strategy.EMERGING_LEADER]
    (found,) = scan_pass(h, risk, core, [()], features=True)
    log(f"core scan with features: {time.monotonic() - clock:.0f} s")
    converged = fixed_points(
        h,
        risk,
        regime_inputs(h, risk),
        [variant],
        track(h, found, risk),
        strategies[Strategy.EMERGING_LEADER],
        macro_stale_limit=risk.data.macro_stale_sessions,
        log=log,
    )[variant]
    spells = correction_spells(regimes_of(converged.results))
    (emerging,) = scan_pass(
        h, risk, [strategies[Strategy.EMERGING_LEADER]], [spells], features=True
    )
    results = {r.session: r for r in converged.results}
    lists = capped_lists(h, found + emerging, regimes_of(converged.results), risk)
    log(f"lists: {time.monotonic() - clock:.0f} s ({sum(map(len, lists.values()))} listings)")
    return PoolInputs(h, results, loaded.calendar, risk), lists


def run_pool_step(
    loaded: Loaded, risk: RiskConfig, variant: Variant, *, count_only: bool
) -> list[PoolTrade]:
    from auratrade.jobs.simulate import _alpaca

    p, lists = pool_inputs(loaded, risk, variant)
    cache = MinuteCache(loaded.store, _alpaca(loaded.settings))

    def symbols_for(wanted: Mapping[date, set[str]]) -> Mapping[tuple[date, str], str]:
        return traded_symbols(loaded.store, wanted)

    def minutes_for(wanted: Mapping[date, set[str]]) -> Mapping[date, Mapping[str, Minutes]]:
        pairs = sum(map(len, wanted.values()))
        _log(f"minute bars wanted: {pairs} symbol-sessions over {len(wanted)} sessions")
        if count_only:
            raise SystemExit(0)
        return prefetch(p, cache, wanted, log=_log)

    return run_pool(p, lists, minutes_for, symbols_for)


def main(argv: Sequence[str] | None = None) -> int:
    from auratrade.jobs.wiring import load_runtime_settings

    parser = argparse.ArgumentParser(prog="python -m backtest.baseline")
    parser.add_argument("step", choices=["regime", "pool"])
    parser.add_argument("--report", type=Path)
    parser.add_argument("--variant", default=CHOSEN, help="the regime variant (pool)")
    parser.add_argument(
        "--count", action="store_true", help="pool: count the minute bars to fetch and stop"
    )
    args = parser.parse_args(argv)
    clock = time.monotonic()
    risk = load_risk_config(load_runtime_settings().risk_config_path)
    loaded = _load(risk)
    h = loaded.h
    if args.step == "pool":
        trades = run_pool_step(loaded, risk, variant_named(args.variant), count_only=args.count)
        DETAIL.mkdir(parents=True, exist_ok=True)
        with (DETAIL / "pool.pickle").open("wb") as f:
            pickle.dump(trades, f)
        report = args.report or POOL_REPORT
        reading = bt.kept_reading(report)
        report.write_text(
            render_pool(trades, risk, args.variant, time.monotonic() - clock, reading),
            encoding="utf-8",
        )
        _log(f"wrote {report}")
        return 0
    report = args.report or REGIME_REPORT
    converged, summaries, chosen, _ = run_regime(h, risk)
    DETAIL.mkdir(parents=True, exist_ok=True)
    for v, c in converged.items():
        bt.write_detail(v.name, c.results, DETAIL / "regime")
    reading = bt.kept_reading(report)
    report.write_text(
        render_regime(h, risk, converged, summaries, chosen, time.monotonic() - clock, reading),
        encoding="utf-8",
    )
    _log(f"chosen {chosen.name}; wrote {report}")
    shares = summaries[chosen].shares
    _log(", ".join(f"{g.value} {shares.get(g, 0):.0%}" for g in Regime))
    return 0


if __name__ == "__main__":
    sys.exit(main())

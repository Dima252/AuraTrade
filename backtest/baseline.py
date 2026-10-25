"""The baseline backtest (task 3.8; README §15): the historical full scan, the breakout tracker,
the 5-pillar regime re-run, then the setup pool and the baseline book.

    uv run python -m backtest.baseline regime    # steps 1-3 -> docs/backtests/regime-5-pillar.md
    uv run python -m backtest.baseline pool      # step 4: the pool and gate -> baseline.md
    uv run python -m backtest.baseline pool --count   # only count the minute bars it would fetch
    uv run python -m backtest.baseline close --blind  # 3.8c: the close entry, counts only
    uv run python -m backtest.baseline close          # 3.8d: the one scored run -> close-entry.md

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
from dataclasses import dataclass, replace
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
from backtest.close_entry import CANDIDATE, CloseRun, run_close
from backtest.close_report import render_close
from backtest.comparisons import Difference, compare
from backtest.comparisons import Variant as CompareVariant
from backtest.comparisons import variants as comparison_variants
from backtest.history_scan import (
    TUNING_END,
    Found,
    History,
    TrackerRow,
    load_history,
    scan_pass,
    track,
)
from backtest.pool import (
    Listing,
    OpeningCheck,
    PoolInputs,
    PoolTrade,
    capped_lists,
    confirm_morning_eps,
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
CLOSE_REPORT = REPO_ROOT / "docs" / "backtests" / "close-entry.md"
BLIND_REPORT = REPO_ROOT / "docs" / "backtests" / "close-entry-blind.md"
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


@dataclass(frozen=True, slots=True)
class Candidates:
    """The pool's setups before the lists: every strategy's, with the EPs in their morning
    view (#143), and the regime each session computed."""

    found: list[Found]
    regimes: dict[date, Regime]


def pool_inputs(
    loaded: Loaded, risk: RiskConfig, variant: Variant, log: Callable[[str], None] = _log
) -> tuple[PoolInputs, Candidates]:
    """The chosen variant's fixed point and the full scan with features. The after-close EP
    scan feeds the breakout tracker (so the regime); the pool gets the morning EPs instead."""
    h = loaded.h
    clock = time.monotonic()
    strategies = build_strategies(risk)
    core = [s for k, s in strategies.items() if k is not Strategy.EMERGING_LEADER]
    (found,) = scan_pass(h, risk, core, [()], features=True)
    (morning,) = scan_pass(
        h, risk, [strategies[Strategy.EPISODIC_PIVOT]], [()], features=True, morning=True
    )
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
    pooled = [f for f in found if f.setup.strategy is not Strategy.EPISODIC_PIVOT]
    return PoolInputs(h, results, loaded.calendar, risk), Candidates(
        pooled + morning + emerging, regimes_of(converged.results)
    )


def pool_lists(
    p: PoolInputs,
    candidates: Candidates,
    minutes_for: Callable[[Mapping[date, set[str]]], Mapping[date, Mapping[str, Minutes]]],
    symbols_for: Callable[[Mapping[date, set[str]]], Mapping[tuple[date, str], str]],
    log: Callable[[str], None] = _log,
) -> tuple[dict[date, list[Listing]], OpeningCheck]:
    """The morning EPs through the 09:36 volume check, then the capped lists."""
    check = confirm_morning_eps(p, candidates.found, minutes_for, symbols_for)
    log(
        f"morning EPs: {check.passed} of {check.candidates} passed the opening volume "
        f"({check.no_minutes} without minute bars)"
    )
    lists = capped_lists(p.h, check.kept, candidates.regimes, p.risk)
    log(f"lists: {sum(map(len, lists.values()))} listings")
    return lists, check


def morning_notes(
    p: PoolInputs, lists: Mapping[date, Sequence[Listing]], check: OpeningCheck
) -> list[str]:
    """The report's lines on the morning EPs (#143): the volume check and the known residual."""
    eps = [
        listing
        for listings in lists.values()
        for listing in listings
        if listing.found.setup.strategy is Strategy.EPISODIC_PIVOT
    ]
    differ = 0
    for listing in eps:
        evening = p.previous_session(listing.scan_day)
        before = p.results.get(evening) if evening is not None else None
        now = p.results.get(listing.scan_day)
        differ += before is not None and now is not None and before.regime is not now.regime
    ratio = p.risk.strategies.EPISODIC_PIVOT.min_opening_volume_ratio
    return [
        f"**EPs at 09:36 (#143):** {check.candidates:,} gaps of 10% or more at the open; "
        f"{check.passed:,} traded at least {ratio:.0%} of their prior average daily volume "
        f"from the open to the entry, {check.no_minutes:,} had no minute bars. Their features, "
        "price and ATR are read at the prior close. Known residual: of the "
        f"{len(eps):,} EP listings, {differ:,} were chosen under a gap-session regime that "
        "differs from the evening before's (the guardrails use the evening before's).",
    ]


@dataclass(frozen=True, slots=True)
class PoolRun:
    reference: list[PoolTrade]
    survivors: list[PoolTrade]
    """The live rule for securities without report dates (rejected): survivors only."""
    compared: list[tuple[CompareVariant, list[Difference], list[PoolTrade]]]
    notes: list[str]


def run_pool_step(
    loaded: Loaded, risk: RiskConfig, variant: Variant, *, count_only: bool, compared: bool
) -> PoolRun:
    from auratrade.jobs.simulate import _alpaca

    p, candidates = pool_inputs(loaded, risk, variant)
    cache = MinuteCache(loaded.store, _alpaca(loaded.settings))
    held: dict[date, dict[str, Minutes]] = {}

    def symbols_for(wanted: Mapping[date, set[str]]) -> Mapping[tuple[date, str], str]:
        return traded_symbols(loaded.store, wanted)

    def minutes_for(wanted: Mapping[date, set[str]]) -> Mapping[date, Mapping[str, Minutes]]:
        missing = {d: s - held.get(d, {}).keys() for d, s in wanted.items()}
        missing = {d: s for d, s in missing.items() if s}
        if missing:
            pairs = sum(map(len, missing.values()))
            _log(f"minute bars wanted: {pairs} symbol-sessions over {len(missing)} sessions")
            if count_only:
                raise SystemExit(0)
            for d, got in prefetch(p, cache, missing, log=_log).items():
                held.setdefault(d, {}).update(got)
        return {d: {x: held[d][x] for x in s if x in held.get(d, {})} for d, s in wanted.items()}

    lists, check = pool_lists(p, candidates, minutes_for, symbols_for)
    clock = time.monotonic()
    reference = run_pool(p, lists, minutes_for, symbols_for)
    _log(f"pool: {time.monotonic() - clock:.0f} s")
    survivors = run_pool(replace(p, reject_without_reports=True), lists, minutes_for, symbols_for)
    out = []
    if compared:
        for v in comparison_variants(risk):
            clock = time.monotonic()
            played = run_pool(replace(p, risk=v.change(risk)), lists, minutes_for, symbols_for)
            out.append((v, compare(v, reference, played), played))
            _log(f"{v.name}: {time.monotonic() - clock:.0f} s")
    return PoolRun(reference, survivors, out, morning_notes(p, lists, check))


def run_close_step(loaded: Loaded, risk: RiskConfig, variant: Variant, *, blind: bool) -> CloseRun:
    """The close-entry candidates (#144): the capped lists with the EPs in their morning view
    and no opening-volume check, played under the `CLOSE` candidate."""
    from auratrade.jobs.simulate import _alpaca

    p, candidates = pool_inputs(loaded, risk.with_candidate(CANDIDATE), variant)
    cache = MinuteCache(loaded.store, _alpaca(loaded.settings))

    def symbols_for(wanted: Mapping[date, set[str]]) -> Mapping[tuple[date, str], str]:
        return traded_symbols(loaded.store, wanted)

    def minutes_for(wanted: Mapping[date, set[str]]) -> Mapping[date, Mapping[str, Minutes]]:
        return prefetch(p, cache, {d: s for d, s in wanted.items() if s}, log=_log)

    lists = capped_lists(p.h, candidates.found, candidates.regimes, p.risk)
    _log(f"lists: {sum(map(len, lists.values()))} listings")
    return run_close(p, lists, minutes_for, symbols_for, blind=blind, log=_log)


def main(argv: Sequence[str] | None = None) -> int:
    from auratrade.jobs.wiring import load_runtime_settings

    parser = argparse.ArgumentParser(prog="python -m backtest.baseline")
    parser.add_argument("step", choices=["regime", "pool", "close"])
    parser.add_argument("--report", type=Path)
    parser.add_argument("--variant", default=CHOSEN, help="the regime variant (pool)")
    parser.add_argument(
        "--count", action="store_true", help="pool: count the minute bars to fetch and stop"
    )
    parser.add_argument(
        "--no-compare", action="store_true", help="pool: the gate only, no comparisons"
    )
    parser.add_argument(
        "--blind", action="store_true", help="close: counts and invariants only (step 3)"
    )
    args = parser.parse_args(argv)
    clock = time.monotonic()
    risk = load_risk_config(load_runtime_settings().risk_config_path)
    loaded = _load(risk)
    h = loaded.h
    if args.step == "close":
        close_run = run_close_step(loaded, risk, variant_named(args.variant), blind=args.blind)
        report = args.report or (BLIND_REPORT if args.blind else CLOSE_REPORT)
        report.write_text(
            render_close(
                close_run,
                risk.with_candidate(CANDIDATE),
                args.variant,
                time.monotonic() - clock,
                bt.kept_reading(report),
            ),
            encoding="utf-8",
        )
        _log(f"wrote {report}")
        if not args.blind:
            DETAIL.mkdir(parents=True, exist_ok=True)
            with (DETAIL / "close.pickle").open("wb") as f:
                pickle.dump({"reference": close_run.reference}, f)
        return 0
    if args.step == "pool":
        run = run_pool_step(
            loaded,
            risk,
            variant_named(args.variant),
            count_only=args.count,
            compared=not args.no_compare,
        )
        report = args.report or POOL_REPORT
        reading = bt.kept_reading(report)
        report.write_text(
            render_pool(
                run.reference,
                risk,
                args.variant,
                time.monotonic() - clock,
                reading,
                survivors=run.survivors,
                compared=[(v, d) for v, d, _ in run.compared],
                notes=run.notes,
            ),
            encoding="utf-8",
        )
        _log(f"wrote {report}")
        # The variants' changes are closures: keep their names with the trades.
        DETAIL.mkdir(parents=True, exist_ok=True)
        saved = {
            "reference": run.reference,
            "survivors": run.survivors,
            "compared": {v.name: played for v, _, played in run.compared},
        }
        with (DETAIL / "pool.pickle").open("wb") as f:
            pickle.dump(saved, f)
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

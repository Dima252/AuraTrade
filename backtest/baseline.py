"""The baseline backtest (task 3.8; README §15): the historical full scan, the breakout tracker,
the 5-pillar regime re-run, then the setup pool and the baseline book.

    uv run python -m backtest.baseline regime    # steps 1-3 -> docs/backtests/regime-5-pillar.md

Everything is fixed in `docs/backtests/baseline-preregistration.md`, written before any of this
ran on real data. **The holdout stays closed:** nothing after `TUNING_END` is loaded until the
chosen regime and defaults are frozen in `risk.yaml` (step 5).
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from auratrade.config import REPO_ROOT, Regime, RiskConfig, Strategy, load_risk_config
from auratrade.strategies.registry import build_strategies
from backtest import regime as bt
from backtest.history_scan import TUNING_END, History, TrackerRow, load_history, scan_pass, track
from backtest.regime_search import VARIANTS, Converged, Variant, choose, fixed_points, regime_inputs

REGIME_REPORT = REPO_ROOT / "docs" / "backtests" / "regime-5-pillar.md"
DETAIL = REPO_ROOT / "var" / "backtests" / "baseline"


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


def _load(risk: RiskConfig) -> History:
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
    return load_history(make_store(settings), days, risk, last=TUNING_END, log=_log)


def main(argv: Sequence[str] | None = None) -> int:
    from auratrade.jobs.wiring import load_runtime_settings

    parser = argparse.ArgumentParser(prog="backtest/baseline.py")
    parser.add_argument("step", choices=["regime"])
    parser.add_argument("--report", type=Path, default=REGIME_REPORT)
    args = parser.parse_args(argv)
    clock = time.monotonic()
    risk = load_risk_config(load_runtime_settings().risk_config_path)
    h = _load(risk)
    converged, summaries, chosen, _ = run_regime(h, risk)
    DETAIL.mkdir(parents=True, exist_ok=True)
    for v, c in converged.items():
        bt.write_detail(v.name, c.results, DETAIL / "regime")
    reading = bt.kept_reading(args.report)
    args.report.write_text(
        render_regime(h, risk, converged, summaries, chosen, time.monotonic() - clock, reading),
        encoding="utf-8",
    )
    _log(f"chosen {chosen.name}; wrote {args.report}")
    shares = summaries[chosen].shares
    _log(", ".join(f"{g.value} {shares.get(g, 0):.0%}" for g in Regime))
    return 0


if __name__ == "__main__":
    sys.exit(main())

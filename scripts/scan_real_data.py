"""Real-data run of the strategy scans (task 3.2a) over the tuning period's point-in-time
universe: what they find, how often, and with what levels.

    uv run python scripts/scan_real_data.py            # writes docs/backtests/scans.md

`VcpBreakout`, `MomentumBreakout` and the `EpisodicPivot` proxy run on every universe member on
every session of 2017-2022 (2016 warms up the 200-day and the 252-session windows), with the
RS rating and momentum rank ranked within that session's universe. **The holdout stays
closed:** nothing after 2022 is loaded. Nothing is tuned here; the counts and levels are for a
human to judge, and the samples are for a visual check against charts. The scans are judged
on trades in the 3.8 baseline backtest. Every setup goes to var/scans/setups.csv (git-ignored).
A `## Reading` section written by hand at the end of the report is kept when it is regenerated.

Not wired here: the EP proxy's earnings flag (it is a fact for the ranking, not a filter) and
half-day volume normalization (a handful of sessions a year).
"""

from __future__ import annotations

import argparse
import csv
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from auratrade.config import REPO_ROOT, RiskConfig, Strategy, load_risk_config
from auratrade.history.read import SecuritySeries, load_membership, load_securities, load_series
from auratrade.indicators.relative_strength import percentile_ranks
from auratrade.storage import ObjectStore
from auratrade.strategies.base import Setup, SymbolData
from auratrade.strategies.episodic_pivot import EpisodicPivotDetail
from auratrade.strategies.momentum_breakout import MomentumDetail
from auratrade.strategies.registry import build_strategies
from auratrade.strategies.trend_template import rs_scores
from auratrade.strategies.vcp_breakout import VcpDetail

REPORT = REPO_ROOT / "docs" / "backtests" / "scans.md"
DETAIL = REPO_ROOT / "var" / "scans"
LOAD_START = date(2016, 1, 1)
FIRST_REPORTED = date(2017, 1, 1)
TUNING_END = date(2022, 12, 30)
SAMPLES = 12
SEED = 3_2


@dataclass(frozen=True, slots=True)
class Found:
    security: str
    symbol: str
    day: date
    setup: Setup
    close: float
    next_high: float | None
    """The next session's high, to see how often the pivot is reached the next day."""


@dataclass(slots=True)
class ScanRun:
    sessions: list[date]
    members: list[int] = field(default_factory=list)
    found: list[Found] = field(default_factory=list)
    exceptions: list[str] = field(default_factory=list)
    seconds: dict[str, float] = field(default_factory=dict)


def ranks_by_security(
    series: Mapping[str, SecuritySeries], members: Mapping[date, set[str]], risk: RiskConfig
) -> tuple[dict[str, list[float | None]], dict[str, list[float | None]]]:
    """Each security's RS rating and best momentum rank per bar, ranked within the session's
    universe (README §5.3). Non-members get None: they are never scanned."""
    lookbacks = risk.strategies.MOMENTUM_BREAKOUT.rank_lookbacks
    rs: dict[date, dict[str, float]] = defaultdict(dict)
    returns: dict[tuple[date, int], dict[str, float]] = defaultdict(dict)
    for key, s in series.items():
        closes = s.close
        for i, (d, score) in enumerate(
            zip(s.dates, rs_scores(closes, risk.trend_template), strict=True)
        ):
            if key not in members.get(d, ()):
                continue
            if score is not None:
                rs[d][key] = score
            for n in lookbacks:
                if i >= n:
                    returns[d, n][key] = closes[i] / closes[i - n] - 1
    rs_pct = {d: percentile_ranks(v) for d, v in rs.items()}
    mom_pct = {k: percentile_ranks(v) for k, v in returns.items()}
    rs_out: dict[str, list[float | None]] = {}
    mom_out: dict[str, list[float | None]] = {}
    for key, s in series.items():
        rs_out[key] = [rs_pct.get(d, {}).get(key) for d in s.dates]
        best: list[float | None] = []
        for d in s.dates:
            values = [r for n in lookbacks if (r := mom_pct.get((d, n), {}).get(key)) is not None]
            best.append(max(values) if values else None)
        mom_out[key] = best
    return rs_out, mom_out


def run_scans(
    store: ObjectStore,
    sessions: Sequence[date],
    risk: RiskConfig,
    *,
    log: Callable[[str], None] = lambda _: None,
) -> ScanRun:
    clock = time.monotonic()
    run = ScanRun([d for d in sessions if d >= FIRST_REPORTED])

    def lap(name: str) -> None:
        nonlocal clock
        run.seconds[name] = round(time.monotonic() - clock, 1)
        clock = time.monotonic()
        log(f"{name}: {run.seconds[name]} s")

    securities = load_securities(store)
    members = load_membership(store, {d.year for d in sessions})
    ever = set().union(*(members.get(d, set()) for d in sessions))
    series = load_series(store, sessions, securities=ever)
    lap("load")
    rs, momentum = ranks_by_security(series, members, risk)
    lap("ranks")
    run.members = [len(members.get(d, ())) for d in run.sessions]
    strategies = build_strategies(risk)
    for key in sorted(series):
        s = series[key]
        symbol = str(securities[key]["symbol"])
        try:
            bars = s.bars()
            data = SymbolData.build(
                symbol, bars, rs_ratings=rs[key], momentum_ranks=momentum[key], risk=risk
            )
            for i, d in enumerate(s.dates):
                if d < FIRST_REPORTED or key not in members.get(d, ()):
                    continue
                for strategy in strategies.values():
                    setup = strategy.scan(data, i)
                    if setup is not None:
                        nxt = bars[i + 1].high if i + 1 < len(bars) else None
                        run.found.append(Found(key, symbol, d, setup, bars[i].close, nxt))
        except Exception as exc:
            run.exceptions.append(f"{key} ({symbol}): {type(exc).__name__}: {exc}")
    lap("scans")
    return run


# --- The report ---------------------------------------------------------------------------------

READING = "## Reading"


def kept_reading(path: Path) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    at = text.find(READING)
    return text[at:].rstrip() + "\n" if at >= 0 else ""


def _q(values: Sequence[float], digits: int = 1, pct: bool = True) -> str:
    """Median and the 10th / 90th percentiles."""
    if not values:
        return "-"
    if len(values) < 10:
        return f"{statistics.median(values) * (100 if pct else 1):.{digits}f}"
    deciles = statistics.quantiles(values, n=10)
    scale = 100 if pct else 1
    unit = "%" if pct else ""
    return (
        f"{statistics.median(values) * scale:.{digits}f}{unit} "
        f"({deciles[0] * scale:.{digits}f}-{deciles[-1] * scale:.{digits}f})"
    )


def _stop_width(f: Found) -> float:
    return 1 - f.setup.structural_stop / f.setup.pivot


def render(run: ScanRun, risk: RiskConfig, reading: str) -> str:
    by: dict[Strategy, list[Found]] = defaultdict(list)
    for f in run.found:
        by[f.setup.strategy].append(f)
    years = sorted({d.year for d in run.sessions})
    order = [Strategy.VCP_BREAKOUT, Strategy.MOMENTUM_BREAKOUT, Strategy.EPISODIC_PIVOT]
    lines = [
        "# Strategy scans on real data (task 3.2a)",
        "",
        f"Generated by `scripts/scan_real_data.py` on {datetime.now(UTC):%Y-%m-%d %H:%M} UTC, "
        f"`risk.yaml` {risk.short_hash}. Sessions {run.sessions[0]} to {run.sessions[-1]} "
        f"({len(run.sessions)}; 2016 is warm-up, the 2023+ holdout is not loaded). Universe "
        f"{min(run.members)}-{max(run.members)} members a session. Exceptions: "
        f"**{len(run.exceptions)}**. Run time: "
        + ", ".join(f"{k} {v:.0f} s" for k, v in run.seconds.items())
        + ".",
        "",
        "Every scan runs on every member every session, whatever the regime (the breakout "
        "tracker needs them all); the regime decides only what is fundable. A setup is a "
        "candidate for the **next** session; the same base usually shows up on several sessions "
        "in a row.",
        "",
        "## Setups per year",
        "",
        "| Strategy | "
        + " | ".join(str(y) for y in years)
        + " | Per session: median (max) | Symbols |",
        "|---|" + "---|" * len(years) + "---|---|",
    ]
    for strategy in order:
        found = by[strategy]
        per_year = Counter(f.day.year for f in found)
        per_session = Counter(f.day for f in found)
        counts = [per_session.get(d, 0) for d in run.sessions]
        lines.append(
            f"| `{strategy.value}` | "
            + " | ".join(f"{per_year.get(y, 0):,}" for y in years)
            + f" | {statistics.median(counts):.0f} ({max(counts)})"
            + f" | {len({f.security for f in found}):,} |"
        )
    vcp_keys = {(f.security, f.day) for f in by[Strategy.VCP_BREAKOUT]}
    both = sum((f.security, f.day) in vcp_keys for f in by[Strategy.MOMENTUM_BREAKOUT])
    lines += [
        "",
        f"Sessions where one stock is both a VCP and a momentum setup: {both:,}.",
        "",
        "## Levels",
        "",
        "Median (10th-90th percentile). The structural stop's width is measured from the pivot; "
        "the order-time limits (8% below the limit for VCP, 1 x ADR for momentum and 1.5 x the "
        "pre-gap ADR for EP, from the trigger) are applied by the guardrails (3.5b), not here. "
        '"Next-day reach": the next session\'s high reached the pivot.',
        "",
        "| Strategy | Stop width | Pivot above the close | Next-day reach |",
        "|---|---|---|---|",
    ]
    for strategy in order:
        found = by[strategy]
        reach = [f.next_high >= f.setup.pivot for f in found if f.next_high is not None]
        # An EP enters on its gap session, so the next session's reach doesn't apply.
        reached = (
            "n/a"
            if strategy is Strategy.EPISODIC_PIVOT
            else f"{(sum(reach) / len(reach) if reach else 0):.0%}"
        )
        lines.append(
            f"| `{strategy.value}` | {_q([_stop_width(f) for f in found])} | "
            f"{_q([f.setup.pivot / f.close - 1 for f in found])} | {reached} |"
        )
    vcp = [
        f.setup.detail for f in by[Strategy.VCP_BREAKOUT] if isinstance(f.setup.detail, VcpDetail)
    ]
    mom = [
        f.setup.detail
        for f in by[Strategy.MOMENTUM_BREAKOUT]
        if isinstance(f.setup.detail, MomentumDetail)
    ]
    ep = [
        f.setup.detail
        for f in by[Strategy.EPISODIC_PIVOT]
        if isinstance(f.setup.detail, EpisodicPivotDetail)
    ]
    contraction_mix = Counter(len(d.contractions) for d in vcp)
    lines += [
        "",
        "## What each scan found",
        "",
        "**`VcpBreakout`:** contractions "
        + ", ".join(f"{n}: {contraction_mix[n]:,}" for n in sorted(contraction_mix))
        + f"; base length {_q([float(d.base_sessions) for d in vcp], 0, pct=False)} sessions; "
        f"first contraction {_q([d.contractions[0].depth for d in vcp])}; final contraction "
        f"{_q([d.contractions[-1].depth for d in vcp])}; final volume ratio "
        f"{_q([d.final_volume_ratio for d in vcp], 2, pct=False)}.",
        "",
        "**`MomentumBreakout`:** consolidation "
        f"{_q([float(d.consolidation_sessions) for d in mom], 0, pct=False)} "
        f"sessions; prior move {_q([d.move for d in mom], 0)}; consolidation depth "
        f"{_q([d.depth for d in mom])}; second-half range vs first "
        f"{_q([d.second_half_range / d.first_half_range for d in mom], 2, pct=False)}.",
        "",
        f"**`EpisodicPivot` (proxy):** gap {_q([d.gap for d in ep])}; volume "
        f"{_q([d.volume_ratio for d in ep], 1, pct=False)} x; gap held into the close (close > "
        f"open) on {sum(d.day_gain > d.gap for d in ep) / max(1, len(ep)):.0%}; prior 6-month move "
        f"{_q([m for d in ep if (m := d.prior_moves.get(126)) is not None], 0)}.",
        "",
        "## Samples for a visual check",
        "",
        f"{SAMPLES} random setups per strategy (seed {SEED}); check each against a daily chart.",
        "",
    ]
    rng = random.Random(SEED)  # noqa: S311 - a reproducible sample, not security
    for strategy in order:
        found = by[strategy]
        picked = sorted(
            rng.sample(found, min(SAMPLES, len(found))), key=lambda f: (f.day, f.symbol)
        )
        lines += [
            f"**`{strategy.value}`**",
            "",
            "| Session | Symbol | Close | Pivot | Stop | Detail |",
            "|---|---|---|---|---|---|",
        ]
        for f in picked:
            facts = f.setup.detail.facts()
            detail = ", ".join(f"{k} {v}" for k, v in facts.items() if k in _SAMPLE_FACTS)
            lines.append(
                f"| {f.day} | {f.symbol} | {f.close:.2f} | {f.setup.pivot:.2f} | "
                f"{f.setup.structural_stop:.2f} | {detail} |"
            )
        lines.append("")
    if run.exceptions:
        lines += ["## Exceptions", "", *(f"- {e}" for e in run.exceptions[:20]), ""]
    lines.append(reading or f"{READING}\n\n(to be written)\n")
    return "\n".join(lines)


_SAMPLE_FACTS = {
    "contraction_depths",
    "base_sessions",
    "final_volume_ratio",
    "consolidation_sessions",
    "prior_move",
    "consolidation_depth",
    "gap",
    "volume_ratio",
}


def write_detail(run: ScanRun, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "setups.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["session", "symbol", "security", "strategy", "close", "pivot", "stop", "facts"]
        )
        for f in run.found:
            writer.writerow(
                [
                    f.day,
                    f.symbol,
                    f.security,
                    f.setup.strategy.value,
                    f.close,
                    f.setup.pivot,
                    f.setup.structural_stop,
                    dict(f.setup.detail.facts()),
                ]
            )


def main(argv: Sequence[str] | None = None) -> int:
    from auratrade.calendar.sessions import load_calendar
    from auratrade.db.engine import engine_from_settings
    from auratrade.jobs.wiring import load_runtime_settings
    from auratrade.storage import make_store

    parser = argparse.ArgumentParser(prog="scripts/scan_real_data.py")
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args(argv)
    settings = load_runtime_settings()
    risk = load_risk_config(settings.risk_config_path)
    engine = engine_from_settings(settings)
    try:
        with engine.connect() as conn:
            calendar = load_calendar(conn)
    finally:
        engine.dispose()
    days = calendar.sessions_between(max(LOAD_START, calendar.first or LOAD_START), TUNING_END)
    run = run_scans(
        make_store(settings),
        [s.day for s in days],
        risk,
        log=lambda line: print(line, file=sys.stderr),
    )
    write_detail(run, DETAIL)
    args.report.write_text(render(run, risk, kept_reading(args.report)), encoding="utf-8")
    print(
        f"wrote {args.report}; setups {len(run.found)}; exceptions {len(run.exceptions)}",
        file=sys.stderr,
    )
    return 1 if run.exceptions else 0


if __name__ == "__main__":
    sys.exit(main())

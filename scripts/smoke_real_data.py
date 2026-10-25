"""Real-data smoke run (task 1.6): the modules built ahead of the data layer, over the whole
backfilled history and the point-in-time universe. Re-run it whenever one of them changes.

    uv run python scripts/smoke_real_data.py            # writes docs/backtests/smoke.md

Indicators (2.1), bases and the base count (2.2), the regime (2.3), the Trend Template (3.1) and
position plans on a few sample trades (3.5a) run on the Parquet history (scripts/backfill.py).
It checks that nothing raises and that no NaN or infinite value appears where the inputs were
complete, and summarises what the modules produce so a human can judge whether it is plausible.
Nothing is tuned here: a clear miss goes to task 2.4 (regime) or the module's own task.
Per-session detail goes to var/smoke/ (git-ignored). A `## Reading` section written by hand at
the end of the report (what the numbers mean) is kept when the report is regenerated.
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from auratrade.config import REPO_ROOT, RiskConfig, Strategy, load_risk_config
from auratrade.history.read import (
    HistoryBar,
    SecuritySeries,
    index_securities,
    load_macro,
    load_membership,
    load_securities,
    load_series,
)
from auratrade.indicators.bases import (
    BaseCountState,
    BaseRules,
    BaseStatus,
    PriceBase,
    detect_bases,
)
from auratrade.indicators.moving_averages import ema, sma
from auratrade.indicators.ranges import highest_high, lowest_low
from auratrade.indicators.relative_strength import percentile_ranks
from auratrade.indicators.volatility import adr_pct, atr
from auratrade.indicators.volume import up_down_volume_ratio
from auratrade.plans.entry import new_plan
from auratrade.plans.levels import trail_levels
from auratrade.plans.model import Day
from auratrade.plans.resolve import resolve
from auratrade.regime.classifier import RegimeResult, classify
from auratrade.regime.inputs import LoadedData, MissingDataError, build_inputs
from auratrade.regime.state import RegimeState
from auratrade.storage import ObjectStore
from auratrade.strategies.trend_template import rs_scores, trend_template

REPORT = REPO_ROOT / "docs" / "backtests" / "smoke.md"
DETAIL = REPO_ROOT / "var" / "smoke"
# A day-over-day move this large in a split-adjusted close is listed for a human to explain.
BIG_MOVE = 0.5
MAX_LISTED = 15
TRADE_SESSIONS = 60
STOP_CAP = 0.08  # the walking skeleton's structural-stop cap (task 1.7), for the sample trades


@dataclass
class Smoke:
    first: date
    last: date
    sessions: int
    securities: dict[str, int] = field(default_factory=dict)
    members: list[int] = field(default_factory=list)  # per session
    member_securities: int = 0
    delisted_members: int = 0
    assumed_type_members: int = 0
    with_cik_members: int = 0
    internal_gaps: int = 0  # member securities with a missing session inside their history
    missing_bars: int = 0
    corrections: int = 0
    exceptions: list[str] = field(default_factory=list)
    non_finite: Counter[str] = field(default_factory=Counter)
    non_finite_examples: list[str] = field(default_factory=list)
    big_moves: list[tuple[float, str, str, date, float, float]] = field(default_factory=list)
    tt_pass: dict[date, int] = field(default_factory=dict)
    tt_rule_rate: dict[int, float] = field(default_factory=dict)
    stages_by_year: dict[int, Counter[int]] = field(default_factory=dict)
    reliable_breakouts: int = 0
    breakouts: int = 0
    failed_bases: int = 0
    high_stage: list[tuple[int, str, str, date, bool]] = field(default_factory=list)
    stage_mix: dict[date, Counter[str]] = field(default_factory=dict)
    regime: list[RegimeResult] = field(default_factory=list)
    regime_error: str | None = None
    trades: list[dict[str, Any]] = field(default_factory=list)
    seconds: dict[str, float] = field(default_factory=dict)


def _universe_type(s: Mapping[str, Any], risk: RiskConfig) -> bool:
    u = risk.universe
    return s["security_type"] in u.security_types and (
        s["exchange"] is None or s["exchange"] in u.exchanges
    )


def _non_finite(
    smoke: Smoke, name: str, key: str, dates: Sequence[date], values: Sequence[float | None]
) -> None:
    for d, v in zip(dates, values, strict=True):
        if v is not None and not math.isfinite(v):
            smoke.non_finite[name] += 1
            if len(smoke.non_finite_examples) < MAX_LISTED:
                smoke.non_finite_examples.append(f"{name} {key} {d}: {v}")


def check_indicators(smoke: Smoke, key: str, s: SecuritySeries, bars: Sequence[HistoryBar]) -> None:
    closes = s.close
    for name, values in (
        ("sma50", sma(closes, 50)),
        ("sma150", sma(closes, 150)),
        ("sma200", sma(closes, 200)),
        ("ema21", ema(closes, 21)),
        ("atr14", atr(bars, 14)),
        ("adr20", adr_pct(bars, 20)),
        ("high252", highest_high(bars, 252)),
        ("low252", lowest_low(bars, 252)),
    ):
        _non_finite(smoke, name, key, s.dates, values)
    # No down volume in the window is +inf by definition; only NaN is a fault here.
    ratio = up_down_volume_ratio(bars, 50)
    _non_finite(smoke, "ud_volume50", key, s.dates, [v if v != math.inf else None for v in ratio])
    for i in range(1, len(s)):
        move = closes[i] / closes[i - 1] - 1
        if abs(move) >= BIG_MOVE:
            raw_move = s.raw_close[i] / s.raw_close[i - 1] - 1
            smoke.big_moves.append((abs(move), key, s.security, s.dates[i], move, raw_move))


def rs_ratings_by_security(
    series: Mapping[str, SecuritySeries],
    members: Mapping[date, set[str]],
    risk: RiskConfig,
) -> dict[str, list[float | None]]:
    """Each security's RS rating per bar: the percentile of its RS score within the session's
    universe (archived README §5.3). Non-members get None (rule 8 fails)."""
    by_session: dict[date, dict[str, float]] = defaultdict(dict)
    for key, s in series.items():
        for d, score in zip(s.dates, rs_scores(s.close, risk.trend_template), strict=True):
            if score is not None and key in members.get(d, ()):
                by_session[d][key] = score
    ratings: dict[date, dict[str, float]] = {d: percentile_ranks(v) for d, v in by_session.items()}
    return {key: [ratings.get(d, {}).get(key) for d in s.dates] for key, s in series.items()}


def _stage_label(stage: int | None) -> str:
    if stage is None:
        return "none"
    return str(stage) if stage < 4 else "4+"


def run_smoke(
    store: ObjectStore,
    sessions: Sequence[date],
    minutes: Mapping[date, int],
    risk: RiskConfig,
    *,
    sample_trades: int = 9,
    log: Callable[[str], None] = lambda _: None,
) -> Smoke:
    clock = time.monotonic()
    smoke = Smoke(sessions[0], sessions[-1], len(sessions))

    def lap(name: str) -> None:
        nonlocal clock
        smoke.seconds[name] = round(time.monotonic() - clock, 1)
        clock = time.monotonic()
        log(f"{name}: {smoke.seconds[name]} s")

    securities = load_securities(store)
    smoke.securities = dict(Counter(str(s["source"]) for s in securities.values()))
    members = load_membership(store, {d.year for d in sessions})
    smoke.members = [len(members.get(d, ())) for d in sessions]
    ever = set().union(*members.values()) if members else set()
    indexes = index_securities(securities, risk.regime.indexes)
    series = load_series(store, sessions, securities=ever | set(indexes.values()))
    lap("load")

    position = {d: i for i, d in enumerate(sessions)}
    smoke.member_securities = len(ever)
    for key in ever:
        info = securities[key]
        smoke.delisted_members += not info["active"]
        smoke.assumed_type_members += bool(info["type_assumed"])
        smoke.with_cik_members += info.get("cik") is not None
        got = series.get(key)
        if got is not None and got.dates:
            span = position[got.dates[-1]] - position[got.dates[0]] + 1
            if span > len(got):
                smoke.internal_gaps += 1
                smoke.missing_bars += span - len(got)

    bars_of = {key: s.bars() for key, s in series.items()}
    for key in ever:
        try:
            check_indicators(smoke, str(securities[key]["symbol"]), series[key], bars_of[key])
        except Exception as exc:
            smoke.exceptions.append(f"indicators {key}: {type(exc).__name__}: {exc}")
    smoke.big_moves.sort(reverse=True)
    lap("indicators")

    ratings = rs_ratings_by_security({k: series[k] for k in ever}, members, risk)
    lap("rs")
    tt_pass: Counter[date] = Counter()
    rule_hits: Counter[int] = Counter()
    member_days = 0
    passes_of: dict[str, list[bool]] = {}
    for key in ever:
        s = series[key]
        try:
            templates = trend_template(bars_of[key], ratings[key], risk.trend_template)
        except Exception as exc:
            smoke.exceptions.append(f"trend_template {key}: {type(exc).__name__}: {exc}")
            continue
        passed = []
        for d, t in zip(s.dates, templates, strict=True):
            ok = t.passes()
            passed.append(ok)
            if key in members.get(d, ()):
                member_days += 1
                tt_pass[d] += ok
                for n in range(1, 9):
                    rule_hits[n] += t.rule(n)
        passes_of[key] = passed
    smoke.tt_pass = {d: tt_pass.get(d, 0) for d in sessions}
    smoke.tt_rule_rate = {n: rule_hits[n] / member_days for n in range(1, 9)} if member_days else {}
    lap("trend_template")

    rules = BaseRules(**risk.bases.model_dump())
    stage_mix: dict[date, Counter[str]] = defaultdict(Counter)
    breakouts: list[tuple[str, PriceBase]] = []
    for key in ever:
        s, sec = series[key], securities[key]
        listed = sec["listed_at"] is not None and sec["listed_at"] == s.dates[0]
        try:
            scan = detect_bases(
                bars_of[key], rules, BaseCountState.at_listing() if listed else None
            )
        except Exception as exc:
            smoke.exceptions.append(f"bases {key}: {type(exc).__name__}: {exc}")
            continue
        current: int | None = None
        passed = passes_of.get(key) or [False] * len(s)
        done = sorted((b for b in scan.bases if b.end is not None), key=lambda b: b.end or 0)
        j = 0
        for i, d in enumerate(s.dates):
            while j < len(done) and (done[j].end or 0) <= i:
                b = done[j]
                current = b.stage if b.status is BaseStatus.BROKEN_OUT else None
                j += 1
            if passed[i] and key in members.get(d, ()):
                stage_mix[d][_stage_label(current)] += 1
        for b in done:
            if b.status is BaseStatus.FAILED:
                smoke.failed_bases += 1
                continue
            day = s.dates[b.end or 0]
            stage = b.stage or 0
            smoke.breakouts += 1
            smoke.reliable_breakouts += b.count_reliable
            smoke.stages_by_year.setdefault(day.year, Counter())[min(stage, 4)] += 1
            if stage >= 5:
                smoke.high_stage.append((stage, str(sec["symbol"]), key, day, b.count_reliable))
            if passed[b.end or 0]:
                breakouts.append((key, b))
    smoke.stage_mix = dict(stage_mix)
    smoke.high_stage.sort(reverse=True)
    lap("bases")

    try:
        smoke.regime = run_regime(series, ever, indexes, store, sessions, minutes, risk)
    except MissingDataError as exc:
        smoke.regime_error = str(exc)
    lap("regime")

    smoke.trades = sample_plans(series, securities, breakouts, risk, sample_trades, smoke)
    lap("plans")
    return smoke


def run_regime(
    series: Mapping[str, SecuritySeries],
    ever: set[str],
    indexes: Mapping[str, str],
    store: ObjectStore,
    sessions: Sequence[date],
    minutes: Mapping[date, int],
    risk: RiskConfig,
) -> list[RegimeResult]:
    """The state machine from the second session (starting in CORRECTION), as the replay does."""
    data = LoadedData(
        sessions=sessions,
        session_minutes=minutes,
        indexes={symbol: series[key].ticker_history() for symbol, key in indexes.items()},
        universe=[series[k].ticker_history() for k in ever],
        macro=load_macro(store),
    )
    state: RegimeState | None = None
    out = []
    for inputs in build_inputs(data, sessions[1:], risk.regime, risk.universe):
        result = classify(
            state, inputs, risk.regime, macro_stale_limit=risk.data.macro_stale_sessions
        )
        out.append(result)
        state = result.state
    return out


def sample_plans(
    series: Mapping[str, SecuritySeries],
    securities: Mapping[str, Mapping[str, Any]],
    breakouts: Sequence[tuple[str, PriceBase]],
    risk: RiskConfig,
    n: int,
    smoke: Smoke,
) -> list[dict[str, Any]]:
    """A few real breakouts of Trend Template stocks (the first of each year, by security key),
    each entered at the breakout close with the base low as the stop (capped at 8%), then run
    through `plans.resolve` until it exits or 60 sessions pass."""
    by_year: dict[int, tuple[str, PriceBase]] = {}
    for key, b in sorted(breakouts, key=lambda kb: (series[kb[0]].dates[kb[1].end or 0], kb[0])):
        year = series[key].dates[b.end or 0].year
        by_year.setdefault(year, (key, b))
    out = []
    for year in sorted(by_year)[:n]:
        key, b = by_year[year]
        s = series[key]
        i = b.end or 0
        entry = s.close[i]
        stop = max(b.low, entry * (1 - STOP_CAP))
        row: dict[str, Any] = {"symbol": securities[key]["symbol"], "entry_day": s.dates[i]}
        try:
            plan = new_plan(
                Strategy.VCP_BREAKOUT,
                pivot=b.pivot,
                entry_price=entry,
                initial_stop=round(stop, 2),
                config=risk.plans,
            )
            shares, high, heaviest_down = 100, s.high[i], None
            row |= {"entry": round(entry, 2), "stop": round(stop, 2), "exit": None}
            for k in range(1, TRADE_SESSIONS + 1):
                t = i + k
                if t >= len(s):
                    row["exit"] = "end of data"
                    break
                high = max(high, s.high[t])
                window = s.volume[max(t - 50, 0) : t]
                day = Day(
                    session=k,
                    price=s.close[t],
                    prior_close=s.close[t - 1],
                    high=s.high[t],
                    low=s.low[t],
                    high_since_entry=high,
                    volume=s.volume[t],
                    avg_volume_50=statistics.fmean(window) if len(window) == 50 else None,
                    heaviest_down_volume=heaviest_down,
                    trail_levels=trail_levels(s.close[: t + 1], risk.plans),
                )
                res = resolve(plan, day, shares=shares, config=risk)
                shares -= res.shares_sold
                plan = res.plan
                if s.close[t] < s.close[t - 1]:
                    heaviest_down = max(heaviest_down or 0.0, s.volume[t])
                if res.exit is not None or shares <= 0:
                    exit_price = (
                        plan.hard_stop if res.exit and res.exit.value == "HARD_STOP" else s.close[t]
                    )
                    row |= {
                        "exit": res.exit.value if res.exit else "SOLD_OUT",
                        "sessions": k,
                        "r": round((exit_price - entry) / plan.risk_per_share, 2),
                        "version": plan.version,
                    }
                    break
            else:
                row |= {
                    "exit": "open after 60",
                    "sessions": TRADE_SESSIONS,
                    "version": plan.version,
                }
                row["r"] = round((s.close[i + TRADE_SESSIONS] - entry) / plan.risk_per_share, 2)
        except Exception as exc:
            smoke.exceptions.append(f"plans {key}: {type(exc).__name__}: {exc}")
            row["exit"] = f"error: {type(exc).__name__}"
        out.append(row)
    return out


# --- the report --------------------------------------------------------------------------


def _pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def regime_by_year(results: Sequence[RegimeResult]) -> dict[int, Counter[str]]:
    out: dict[int, Counter[str]] = defaultdict(Counter)
    for r in results:
        out[r.session.year][r.regime.value] += 1
    return dict(out)


def regime_runs(
    results: Sequence[RegimeResult], start: date, end: date
) -> list[tuple[date, date, str, int]]:
    """(first, last, regime, sessions) of each unbroken stretch between `start` and `end`."""
    runs: list[tuple[date, date, str, int]] = []
    for r in results:
        if not start <= r.session <= end:
            continue
        if runs and runs[-1][2] == r.regime.value:
            first, _, regime, n = runs[-1]
            runs[-1] = (first, r.session, regime, n + 1)
        else:
            runs.append((r.session, r.session, r.regime.value, 1))
    return runs


def render(smoke: Smoke, risk: RiskConfig, reading: str = "") -> str:
    """The report; `reading` (the hand-written section of an earlier report) goes at the end."""
    regimes = ("CORRECTION", "EARLY_UPTREND", "UNDER_PRESSURE", "CONFIRMED_UPTREND")
    warm = risk.universe.min_history_sessions
    settled = smoke.members[warm - 1 :]
    lines = [
        "# Real-data smoke run (task 1.6)",
        "",
        f"**Run:** {datetime.now(UTC):%Y-%m-%d %H:%M} UTC with "
        f"`uv run python scripts/smoke_real_data.py`, risk.yaml `{risk.short_hash}`. The modules "
        "built ahead of the data layer (2.1 indicators, 2.2 bases, 2.3 regime, 3.1 Trend "
        "Template, 3.5a plans) over the backfilled history. Nothing is tuned here.",
        "",
        "## Inputs",
        "",
        f"- Sessions {smoke.first} to {smoke.last}: {smoke.sessions}.",
        f"- Securities in the history by source: {dict(sorted(smoke.securities.items()))}.",
        f"- **Universe** (README §5.1, point in time): {smoke.member_securities} securities were "
        f"in it at some session; {smoke.delisted_members} of them "
        f"({_pct(smoke.delisted_members / max(smoke.member_securities, 1))}) no longer trade, and "
        f"{smoke.assumed_type_members} were found only by the ticker sweep (type assumed common).",
        f"- Universe size per session after the first {warm} (no security has "
        f"{warm} bars before then): min {min(settled, default=0)}, median "
        f"{statistics.median(settled) if settled else 0:.0f}, max {max(settled, default=0)}.",
        f"- With a CIK (earnings dates and fundamentals): {smoke.with_cik_members} of "
        f"{smoke.member_securities} "
        f"({_pct(smoke.with_cik_members / max(smoke.member_securities, 1))}).",
        f"- Securities with a missing session inside their history (a halt, under 31 sessions): "
        f"{smoke.internal_gaps}, {smoke.missing_bars} bars in all. Indicators count bars, so a "
        "window over a gap spans one session more.",
        "",
        "## Checks",
        "",
        f"- **Exceptions:** {len(smoke.exceptions)}."
        + ("" if not smoke.exceptions else " First ones:"),
    ]
    lines += [f"  - `{e}`" for e in smoke.exceptions[:MAX_LISTED]]
    total_nf = sum(smoke.non_finite.values())
    lines.append(f"- **NaN or infinite values** where the inputs were complete: {total_nf}.")
    lines += [f"  - `{e}`" for e in smoke.non_finite_examples]
    lines += [
        f"- **Day moves of {BIG_MOVE:.0%} or more** in a universe security's split-adjusted close: "
        f"{len(smoke.big_moves)}. The largest are below with the raw close's move: equal moves "
        "are real trading (a crash or a squeeze); a split-adjusted move with a flat raw close "
        "would be a missed split.",
        "",
        "| Symbol | Session | Close move | Raw close move |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {sym} | {d} | {move:+.0%} | {raw:+.0%} |"
        for _, sym, _, d, move, raw in smoke.big_moves[:MAX_LISTED]
    ]

    lines += ["", "## Regime (2.3)", ""]
    if smoke.regime_error:
        lines.append(f"**Not computed:** {smoke.regime_error}")
    else:
        lines += [
            "Sessions per regime and year (the state machine starts in CORRECTION on the second "
            "session; the first year is warm-up, as the 200-day average and the 252-session "
            "breadth windows fill). Leadership health scores 0 until the breakout tracker (3.7).",
            "",
            "| Year | " + " | ".join(regimes) + " |",
            "|---|" + "---|" * len(regimes),
        ]
        for year, per_regime in sorted(regime_by_year(smoke.regime).items()):
            lines.append(
                f"| {year} | " + " | ".join(str(per_regime.get(r, 0)) for r in regimes) + " |"
            )
        for title, a, b in (
            ("The Feb-Mar 2020 crash", date(2020, 1, 15), date(2020, 6, 30)),
            ("The 2022 bear market", date(2021, 11, 1), date(2023, 3, 31)),
        ):
            lines += ["", f"**{title}** ({a} to {b}): unbroken stretches.", ""]
            lines += ["| From | To | Regime | Sessions |", "|---|---|---|---|"]
            lines += [
                f"| {f} | {t} | {r} | {n} |" for f, t, r, n in regime_runs(smoke.regime, a, b)
            ]

    lines += [
        "",
        "## Trend Template (3.1)",
        "",
        "Universe members passing all 8 rules per session, by year (RS rating = the percentile of "
        "the RS score within that session's universe):",
        "",
        "| Year | Min | Median | Max |",
        "|---|---|---|---|",
    ]
    by_year: dict[int, list[int]] = defaultdict(list)
    for d, n in smoke.tt_pass.items():
        by_year[d.year].append(n)
    for year, counts in sorted(by_year.items()):
        lines.append(
            f"| {year} | {min(counts)} | {statistics.median(counts):.0f} | {max(counts)} |"
        )
    if smoke.tt_rule_rate:
        rates = ", ".join(f"{n}: {_pct(r)}" for n, r in smoke.tt_rule_rate.items())
        lines += ["", f"Share of member-sessions passing each rule: {rates}."]

    lines += [
        "",
        "## Bases and the base count (2.2)",
        "",
        f"- Breakouts: {smoke.breakouts} "
        f"({_pct(smoke.reliable_breakouts / max(smoke.breakouts, 1))} with a reliable count); "
        f"failed candidates: {smoke.failed_bases}.",
        "",
        "| Year | Stage 1 | 2 | 3 | 4+ |",
        "|---|---|---|---|---|",
    ]
    for year, st in sorted(smoke.stages_by_year.items()):
        lines.append(
            f"| {year} | {st.get(1, 0)} | {st.get(2, 0)} | {st.get(3, 0)} | {st.get(4, 0)} |"
        )
    mix: Counter[str] = Counter()
    for day_mix in smoke.stage_mix.values():
        mix.update(day_mix)
    total = sum(mix.values())
    if total:
        shares = ", ".join(f"{k}: {_pct(v / total)}" for k, v in sorted(mix.items()))
        lines += [
            "",
            "Trend Template passers by the stage of their last breakout, over all sessions: "
            f"{shares}.",
        ]
    lines += [
        "",
        f"Stage 5 and above (outliers to explain): {len(smoke.high_stage)}. The highest:",
        "",
        "| Stage | Symbol | Breakout | Count reliable |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {stage} | {sym} | {d} | {'yes' if rel else 'no'} |"
        for stage, sym, _, d, rel in smoke.high_stage[:MAX_LISTED]
    ]

    lines += [
        "",
        "## Position plans (3.5a): sample trades",
        "",
        "The first Trend Template breakout of each year, entered at the breakout close, stop at "
        f"the base low capped at {STOP_CAP:.0%}, default VCP plan, run through `plans.resolve` "
        f"for up to {TRADE_SESSIONS} sessions. A check that the engine runs on real bars, not a "
        "result.",
        "",
        "| Symbol | Entry day | Entry | Stop | Exit | Sessions | R | Plan versions |",
        "|---|---|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {t['symbol']} | {t['entry_day']} | {t.get('entry', '')} | {t.get('stop', '')} | "
        f"{t['exit']} | {t.get('sessions', '')} | {t.get('r', '')} | {t.get('version', '')} |"
        for t in smoke.trades
    ]
    lines += ["", "Run time (s): " + ", ".join(f"{k} {v}" for k, v in smoke.seconds.items()), ""]
    text = "\n".join(lines)
    return f"{text}\n{reading.strip()}\n" if reading.strip() else text


READING = "## Reading"


def kept_reading(path: Path) -> str:
    """The hand-written part of an existing report: from its `## Reading` heading on."""
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    at = text.find(f"\n{READING}")
    return text[at + 1 :] if at >= 0 else ""


def write_detail(smoke: Smoke, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "regime.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["session", "regime", "score", "trend", "breadth", "volatility", "distribution"])
        for r in smoke.regime:
            p = r.pillars
            w.writerow(
                [
                    r.session,
                    r.regime.value,
                    r.score,
                    p.index_trend,
                    p.breadth,
                    p.volatility,
                    p.distribution,
                ]
            )
    with (folder / "trend_template.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            ["session", "members", "tt_pass", "stage1", "stage2", "stage3", "stage4+", "none"]
        )
        for (d, n), members in zip(sorted(smoke.tt_pass.items()), smoke.members, strict=True):
            mix = smoke.stage_mix.get(d, Counter())
            w.writerow([d, members, n, *(mix.get(k, 0) for k in ("1", "2", "3", "4+", "none"))])


def main(argv: Sequence[str] | None = None) -> int:
    from auratrade.calendar.sessions import load_calendar
    from auratrade.db.engine import engine_from_settings
    from auratrade.history.store import bar_sessions
    from auratrade.jobs.wiring import load_runtime_settings
    from auratrade.storage import make_store

    parser = argparse.ArgumentParser(prog="scripts/smoke_real_data.py")
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args(argv)
    settings = load_runtime_settings()
    risk = load_risk_config(settings.risk_config_path)
    store = make_store(settings)
    stored = bar_sessions(store)
    if not stored:
        print("no history: run scripts/backfill.py first", file=sys.stderr)
        return 2
    engine = engine_from_settings(settings)
    try:
        with engine.connect() as conn:
            calendar = load_calendar(conn)
    finally:
        engine.dispose()
    days = calendar.sessions_between(stored[0], stored[-1])
    smoke = run_smoke(
        store,
        [s.day for s in days],
        {s.day: s.minutes for s in days},
        risk,
        log=lambda line: print(line, file=sys.stderr),
    )
    write_detail(smoke, DETAIL)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report = render(smoke, risk, kept_reading(args.report))
    args.report.write_text(report, encoding="utf-8")
    print(f"wrote {args.report}; exceptions {len(smoke.exceptions)}", file=sys.stderr)
    return 1 if smoke.exceptions or sum(smoke.non_finite.values()) else 0


if __name__ == "__main__":
    sys.exit(main())

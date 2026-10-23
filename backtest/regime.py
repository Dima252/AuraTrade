"""The regime backtest (task 2.4; README §5.2, §15): the 4-pillar regime over the tuning period,
the comparisons the plan names, and the report `docs/backtests/regime.md`.

    uv run python backtest/regime.py

**The holdout stays closed.** Nothing after `TUNING_END` is loaded: not the bars, not the macro
series, not the forward returns of the last labels (they stop at the last loaded close). The
2023+ period is opened once, for the 5-pillar model, in the 3.8 re-run. Leadership health
needs the breakout tracker (3.7), so it scores 0 throughout.

Every run replays the live pure functions (`regime.inputs.build_inputs`, `regime.classifier`)
from the second session in `CORRECTION`, as `regime.compute.replay` does. The inputs don't
depend on anything a variant changes, so they are built once and each variant only re-runs the
state machine.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import itertools
import math
import statistics
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from auratrade.calendar.sessions import Session
from auratrade.config import REPO_ROOT, Regime, RegimeConfig, RiskConfig, load_risk_config
from auratrade.history.read import (
    index_securities,
    load_macro,
    load_membership,
    load_securities,
    load_series,
)
from auratrade.regime.classifier import RegimeResult, SessionInputs, classify
from auratrade.regime.inputs import LoadedData, MacroSeries, build_inputs
from auratrade.regime.state import RegimeState
from auratrade.storage import ObjectStore

REPORT = REPO_ROOT / "docs" / "backtests" / "regime.md"
CHART = REPO_ROOT / "docs" / "backtests" / "regime-timeline.svg"
DETAIL = REPO_ROOT / "var" / "backtests" / "regime"

TUNING_START = date(2016, 1, 1)
TUNING_END = date(2022, 12, 30)
FIRST_REPORTED = date(2017, 1, 1)
"""2016 is warm-up: the 200-day average and the 252-session breadth windows fill (README §15)."""

ORDER = (
    Regime.CORRECTION,
    Regime.EARLY_UPTREND,
    Regime.UNDER_PRESSURE,
    Regime.CONFIRMED_UPTREND,
)
SHORT = {
    Regime.CORRECTION: "CORR",
    Regime.EARLY_UPTREND: "EU",
    Regime.UNDER_PRESSURE: "UP",
    Regime.CONFIRMED_UPTREND: "CU",
}
HORIZONS = (5, 20, 60)

# --- Pre-registered before the first run on real data (plan 2.4 "Accept") ---------------------


@dataclass(frozen=True, slots=True)
class Window:
    name: str
    start: date
    end: date


@dataclass(frozen=True, slots=True)
class Decline(Window):
    """SPY's closing peak (`start`) and closing low (`end`) of a known decline."""


DECLINES = (
    Decline("Q4 2018", date(2018, 9, 20), date(2018, 12, 24)),
    Decline("Feb-Mar 2020", date(2020, 2, 19), date(2020, 3, 23)),
    Decline("2022", date(2022, 1, 3), date(2022, 10, 12)),
)
EARLY_SHARE = 0.5
"""'Defensive early in the decline': `CORRECTION` is reached while SPY's drawdown from the
peak is still under half of the decline's full peak-to-low drawdown."""

UPTRENDS = (
    Window("2017", date(2017, 1, 3), date(2017, 12, 29)),
    Window("2019", date(2019, 1, 2), date(2019, 12, 31)),
    Window("H2 2020-2021", date(2020, 7, 1), date(2021, 12, 31)),
)
TARGET_CONFIRMED_SHARE = 0.60
"""`CONFIRMED_UPTREND` covers at least 60% of the sessions of each strong uptrend window."""

RECOVERY_LOW = date(2020, 3, 23)
RECOVERY_EARLY_MAX = 15
RECOVERY_CONFIRMED_MAX = 60
"""'Picked up with reasonable lag': `EARLY_UPTREND` within 15 sessions of the 2020 low and
`CONFIRMED_UPTREND` within 60 (IBD's follow-through day came on day 8, 2020-04-02)."""

MAX_SWITCHES_PER_YEAR = 12
MAX_FALSE_CORRECTIONS_PER_YEAR = 1
FALSE_CORRECTION_MIN_SESSIONS = 16
"""README §5.2: a `CORRECTION` spell of more than 15 sessions ..."""
FALSE_CORRECTION_DRAWDOWN = 0.08
"""... in which SPY never closes 8% or more below its prior 52-week high."""
HIGH_SESSIONS = 252


# --- Variants -----------------------------------------------------------------------------------


def _transitions(cfg: RegimeConfig, **changes: Any) -> RegimeConfig:
    return cfg.model_copy(update={"transitions": cfg.transitions.model_copy(update=changes)})


def _distribution(cfg: RegimeConfig, **changes: Any) -> RegimeConfig:
    return cfg.model_copy(update={"distribution": cfg.distribution.model_copy(update=changes)})


def ibd_published(cfg: RegimeConfig) -> RegimeConfig:
    """IBD's published thresholds, as risk.yaml had them before #135: `CORRECTION` at <= -1,
    `CONFIRMED_UPTREND` (and the `no_ftd` fallback) from +3, distribution -1 from 6 days."""
    return _distribution(
        _transitions(
            cfg,
            correction_max_score=-1,
            confirmed_min_score=3,
            debounce_score=2,
            no_ftd_min_score=3,
        ),
        down_min_days=6,
    )


VARIANTS: dict[str, tuple[str, Callable[[RegimeConfig], RegimeConfig]]] = {
    "baseline": (
        "risk.yaml as committed: the provisional 4-pillar thresholds adopted after the first run "
        "(#135): `CORRECTION` at <= -2, distribution -1 from 8 days, `CONFIRMED_UPTREND` from +2",
        lambda c: c,
    ),
    "ibd_published": (
        "IBD's published thresholds (the first run's baseline): `CORRECTION` at <= -1, "
        "distribution -1 from 6 days, `CONFIRMED_UPTREND` from +3",
        ibd_published,
    ),
    # The plan's comparisons, each against the baseline.
    "exclude_expirations": (
        "distribution days on expiration / rebalance sessions not counted",
        lambda c: _distribution(c, exclude_expiration_sessions=True),
    ),
    "no_ftd_5": (
        "`no_ftd` fallback after 5 sessions",
        lambda c: _transitions(c, no_ftd_sessions=5),
    ),
    "no_ftd_15": (
        "`no_ftd` fallback after 15 sessions",
        lambda c: _transitions(c, no_ftd_sessions=15),
    ),
    "no_ftd_20": (
        "`no_ftd` fallback after 20 sessions",
        lambda c: _transitions(c, no_ftd_sessions=20),
    ),
    "no_eu_minimum": (
        "no 10-session minimum in `EARLY_UPTREND` (1: the 3-session upgrade bounds it)",
        lambda c: _transitions(c, early_uptrend_min_sessions=1),
    ),
    "correction_2": (
        "`CORRECTION` after 2 sessions at its score (index trend at -1 still immediate)",
        lambda c: _transitions(c, correction_sessions=2),
    ),
    # Diagnostics from the first run (not pre-registered): each change alone, on IBD's
    # published thresholds. The three that helped together are the baseline.
    "diag_correction_-2": (
        "diagnostic, on `ibd_published`: `CORRECTION` at <= -2",
        lambda c: _transitions(ibd_published(c), correction_max_score=-2),
    ),
    "diag_distribution_8": (
        "diagnostic, on `ibd_published`: distribution -1 from 8 days",
        lambda c: _distribution(ibd_published(c), down_min_days=8),
    ),
    "diag_vix_ratio_1.1": (
        "diagnostic, on `ibd_published`: volatility -1 from VIX/VIX3M > 1.1 (not 1.0)",
        lambda c: ibd_published(c).model_copy(
            update={"volatility": c.volatility.model_copy(update={"down_min_ratio": 1.1})}
        ),
    ),
    "diag_confirmed_+2": (
        "diagnostic, on `ibd_published`: `CONFIRMED_UPTREND` and the `no_ftd` fallback from +2",
        lambda c: _transitions(
            ibd_published(c), confirmed_min_score=2, debounce_score=1, no_ftd_min_score=2
        ),
    ),
}


# --- Running -------------------------------------------------------------------------------------


def run(
    inputs: Sequence[SessionInputs], cfg: RegimeConfig, macro_stale_limit: int
) -> list[RegimeResult]:
    state: RegimeState | None = None
    out = []
    for i in inputs:
        result = classify(state, i, cfg, macro_stale_limit=macro_stale_limit)
        out.append(result)
        state = result.state
    return out


# --- Measures (pure) ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Spell:
    regime: Regime
    first: date
    last: date
    sessions: int
    no_ftd: bool
    next: Regime | None
    """The regime after the spell; None if it runs to the end of the period."""


def spells(results: Sequence[RegimeResult]) -> list[Spell]:
    out: list[Spell] = []
    start = 0
    for k in range(1, len(results) + 1):
        if k < len(results) and results[k].regime is results[start].regime:
            continue
        first = results[start]
        out.append(
            Spell(
                first.regime,
                first.session,
                results[k - 1].session,
                k - start,
                first.state.no_ftd,
                results[k].regime if k < len(results) else None,
            )
        )
        start = k
    return out


def reported(results: Sequence[RegimeResult]) -> list[RegimeResult]:
    return [r for r in results if r.session >= FIRST_REPORTED]


def per_year(results: Sequence[RegimeResult]) -> dict[int, Counter[Regime]]:
    out: dict[int, Counter[Regime]] = {}
    for r in results:
        out.setdefault(r.session.year, Counter())[r.regime] += 1
    return out


def transitions(results: Sequence[RegimeResult]) -> Counter[tuple[Regime, Regime]]:
    return Counter(
        (prev.regime, r.regime)
        for prev, r in itertools.pairwise(results)
        if r.regime is not prev.regime
    )


def failed_ftd_switches(results: Sequence[RegimeResult]) -> dict[int, int]:
    """Per year, the switches of failed follow-through days: into an `EARLY_UPTREND` spell from
    `CORRECTION` and back to `CORRECTION` (#135 counts these apart from the switch limit)."""
    out: Counter[int] = Counter()
    sp = spells(results)
    for i, s in enumerate(sp):
        if s.regime is not Regime.EARLY_UPTREND or s.next is not Regime.CORRECTION:
            continue
        if i > 0 and sp[i - 1].regime is Regime.CORRECTION:
            out[s.first.year] += 1
        out[sp[i + 1].first.year] += 1
    return dict(out)


def switches_per_year(results: Sequence[RegimeResult]) -> dict[int, int]:
    out: Counter[int] = Counter()
    for prev, r in itertools.pairwise(results):
        out[r.session.year] += r.regime is not prev.regime
    return {year: out[year] for year in sorted({r.session.year for r in results})}


@dataclass(frozen=True, slots=True)
class Spy:
    """SPY's closes over the loaded sessions, oldest first."""

    dates: Sequence[date]
    closes: Sequence[float]

    def index(self, day: date) -> int:
        i = bisect.bisect_left(self.dates, day)
        if i == len(self.dates) or self.dates[i] != day:
            raise KeyError(day)
        return i

    def close(self, day: date) -> float:
        return self.closes[self.index(day)]

    def prior_high(self, day: date) -> float:
        """The highest close of the 252 sessions up to and including ``day``."""
        i = self.index(day)
        return max(self.closes[max(0, i - HIGH_SESSIONS + 1) : i + 1])


@dataclass(frozen=True, slots=True)
class Forward:
    n: int
    mean: float
    median: float
    positive: float
    mean_drawdown: float
    """The mean of the worst close over the horizon vs the label's close."""


def forward_stats(
    results: Sequence[RegimeResult], spy: Spy, horizon: int
) -> dict[Regime, Forward | None]:
    """SPY from the close of each label's session (the label governs the next session) to
    ``horizon`` sessions later; labels whose horizon passes the last loaded close are left out."""
    returns: dict[Regime, list[float]] = {r: [] for r in ORDER}
    drawdowns: dict[Regime, list[float]] = {r: [] for r in ORDER}
    for r in results:
        i = spy.index(r.session)
        if i + horizon >= len(spy.closes):
            continue
        base = spy.closes[i]
        path = spy.closes[i + 1 : i + horizon + 1]
        returns[r.regime].append(path[-1] / base - 1)
        drawdowns[r.regime].append(min(0.0, min(path) / base - 1))
    out: dict[Regime, Forward | None] = {}
    for regime in ORDER:
        xs = returns[regime]
        out[regime] = (
            Forward(
                len(xs),
                statistics.fmean(xs),
                statistics.median(xs),
                sum(x > 0 for x in xs) / len(xs),
                statistics.fmean(drawdowns[regime]),
            )
            if xs
            else None
        )
    return out


PILLARS = ("index_trend", "breadth", "volatility", "distribution", "leadership")


def pillar_counts(results: Sequence[RegimeResult]) -> dict[str, Counter[int]]:
    out = {p: Counter[int]() for p in PILLARS}
    for r in results:
        for p in PILLARS:
            out[p][getattr(r.pillars, p)] += 1
    return out


def score_counts(results: Sequence[RegimeResult]) -> Counter[int]:
    return Counter(r.score for r in results)


def false_corrections(results: Sequence[RegimeResult], spy: Spy) -> list[tuple[Spell, float]]:
    """`CORRECTION` spells longer than 15 sessions in which SPY never closed 8% or more below
    its prior 52-week high, with the spell's deepest such drawdown."""
    out = []
    for s in spells(results):
        if s.regime is not Regime.CORRECTION or s.sessions < FALSE_CORRECTION_MIN_SESSIONS:
            continue
        days = spy.dates[spy.index(s.first) : spy.index(s.last) + 1]
        if any(spy.close(d) <= spy.prior_high(d) * (1 - FALSE_CORRECTION_DRAWDOWN) for d in days):
            continue
        out.append((s, min(spy.close(d) / spy.prior_high(d) - 1 for d in days)))
    return out


def window_shares(results: Sequence[RegimeResult], window: Window) -> dict[Regime, float]:
    inside = [r.regime for r in results if window.start <= r.session <= window.end]
    counts = Counter(inside)
    return {regime: counts[regime] / len(inside) if inside else 0.0 for regime in ORDER}


def window_scores(results: Sequence[RegimeResult], window: Window) -> Counter[int]:
    return Counter(r.score for r in results if window.start <= r.session <= window.end)


@dataclass(frozen=True, slots=True)
class DeclineCheck:
    decline: Decline
    full_drawdown: float
    defensive: date | None
    """The first session from the peak on labelled below `CONFIRMED_UPTREND`."""
    defensive_drawdown: float | None
    correction: date | None
    """The first session from the peak on labelled `CORRECTION` (or already in it)."""
    correction_drawdown: float | None

    @property
    def early(self) -> bool:
        return (
            self.correction_drawdown is not None
            and self.correction_drawdown > EARLY_SHARE * self.full_drawdown
        )


def decline_check(results: Sequence[RegimeResult], spy: Spy, decline: Decline) -> DeclineCheck:
    peak = spy.close(decline.start)
    inside = [r for r in results if decline.start <= r.session <= decline.end]

    def first(pred: Callable[[Regime], bool]) -> tuple[date | None, float | None]:
        for r in inside:
            if pred(r.regime):
                return r.session, spy.close(r.session) / peak - 1
        return None, None

    defensive, defensive_dd = first(lambda g: g is not Regime.CONFIRMED_UPTREND)
    correction, correction_dd = first(lambda g: g is Regime.CORRECTION)
    return DeclineCheck(
        decline,
        spy.close(decline.end) / peak - 1,
        defensive,
        defensive_dd,
        correction,
        correction_dd,
    )


@dataclass(frozen=True, slots=True)
class Recovery:
    early: int | None
    early_gain: float | None
    confirmed: int | None
    confirmed_gain: float | None

    @property
    def ok(self) -> bool:
        return (
            self.early is not None
            and self.early <= RECOVERY_EARLY_MAX
            and self.confirmed is not None
            and self.confirmed <= RECOVERY_CONFIRMED_MAX
        )


def recovery(results: Sequence[RegimeResult], spy: Spy, low: date) -> Recovery:
    """Sessions after ``low`` to the first `EARLY_UPTREND` (or better) and `CONFIRMED_UPTREND`
    label, with SPY's gain from the low's close by then."""
    base = spy.close(low)
    after = [r for r in results if r.session > low]
    early = next((k + 1 for k, r in enumerate(after) if r.regime is not Regime.CORRECTION), None)
    confirmed = next(
        (k + 1 for k, r in enumerate(after) if r.regime is Regime.CONFIRMED_UPTREND), None
    )

    def gain(k: int | None) -> float | None:
        return None if k is None else spy.close(after[k - 1].session) / base - 1

    return Recovery(early, gain(early), confirmed, gain(confirmed))


@dataclass(frozen=True, slots=True)
class EarlySpell:
    spell: Spell
    spy_return: float
    """SPY over the sessions the spell governed: from the close of its first label to the close
    of the session after its last (a label governs the next session)."""


def early_uptrends(results: Sequence[RegimeResult], spy: Spy) -> list[EarlySpell]:
    out = []
    for s in spells(results):
        if s.regime is not Regime.EARLY_UPTREND:
            continue
        end = min(spy.index(s.last) + 1, len(spy.closes) - 1)
        out.append(EarlySpell(s, spy.closes[end] / spy.close(s.first) - 1))
    return out


# --- The summary each variant is compared on -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Summary:
    name: str
    description: str
    shares: dict[Regime, float]
    switches: dict[int, int]
    failed_ftd: dict[int, int]
    false: list[tuple[Spell, float]]
    uptrends: dict[str, dict[Regime, float]]
    declines: list[DeclineCheck]
    recovery: Recovery
    early: list[EarlySpell]
    years: int

    @property
    def mean_switches(self) -> float:
        return statistics.fmean(self.switches.values()) if self.switches else 0.0

    @property
    def max_switches(self) -> int:
        return max(self.switches.values(), default=0)

    @property
    def max_other_switches(self) -> int:
        """The most switches in a year besides failed follow-through days' round trips."""
        return max(
            (n - self.failed_ftd.get(year, 0) for year, n in self.switches.items()), default=0
        )

    @property
    def failed_early(self) -> list[EarlySpell]:
        return [e for e in self.early if e.spell.next is Regime.CORRECTION]

    def checks(self) -> dict[str, bool]:
        return {
            "declines early": all(d.early for d in self.declines),
            "2020 recovery": self.recovery.ok,
            "switches <= 12/yr, failed FTDs apart": self.max_other_switches
            <= MAX_SWITCHES_PER_YEAR,
            "CU share >= target": all(
                u[Regime.CONFIRMED_UPTREND] >= TARGET_CONFIRMED_SHARE
                for u in self.uptrends.values()
            ),
            "false CORR <= 1/yr": len(self.false) <= MAX_FALSE_CORRECTIONS_PER_YEAR * self.years,
        }


def summarize(name: str, description: str, results: Sequence[RegimeResult], spy: Spy) -> Summary:
    shown = reported(results)
    counts = Counter(r.regime for r in shown)
    return Summary(
        name=name,
        description=description,
        shares={g: counts[g] / len(shown) for g in ORDER} if shown else {},
        switches=switches_per_year(shown),
        failed_ftd=failed_ftd_switches(shown),
        false=false_corrections(shown, spy),
        uptrends={w.name: window_shares(shown, w) for w in UPTRENDS},
        declines=[decline_check(shown, spy, d) for d in DECLINES],
        recovery=recovery(shown, spy, RECOVERY_LOW),
        early=early_uptrends(shown, spy),
        years=len({r.session.year for r in shown}),
    )


# --- Loading (the tuning period only) -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Loaded:
    data: LoadedData
    spy: Spy
    members: int
    delisted_members: int


def load(store: ObjectStore, days: Sequence[Session], risk: RiskConfig) -> Loaded:
    """The history from `TUNING_START` to `TUNING_END` and nothing after it."""
    sessions = [s.day for s in days if TUNING_START <= s.day <= TUNING_END]
    wanted = set(sessions)
    minutes = {s.day: s.minutes for s in days if s.day in wanted}
    securities = load_securities(store)
    membership = load_membership(store, {d.year for d in sessions})
    ever: set[str] = set().union(*(membership.get(d, set()) for d in sessions))
    indexes = index_securities(securities, risk.regime.indexes)
    series = load_series(store, sessions, securities=ever | set(indexes.values()))
    macro = {
        name: MacroSeries(
            [d for d in m.dates if d <= TUNING_END],
            [v for d, v in zip(m.dates, m.values, strict=True) if d <= TUNING_END],
        )
        for name, m in load_macro(store).items()
    }
    data = LoadedData(
        sessions=sessions,
        session_minutes=minutes,
        indexes={symbol: series[key].ticker_history() for symbol, key in indexes.items()},
        universe=[series[k].ticker_history() for k in sorted(ever) if k in series],
        macro=macro,
    )
    spy_series = series[indexes["SPY"]]
    return Loaded(
        data,
        Spy(spy_series.dates, list(spy_series.close)),
        len(ever),
        sum(not securities[k]["active"] for k in ever),
    )


# --- The report ---------------------------------------------------------------------------------

READING = "## Reading"


def _pct(x: float | None, digits: int = 1) -> str:
    return "-" if x is None else f"{x * 100:.{digits}f}%"


def _signed(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:+.1f}%"


def _yes(ok: bool) -> str:
    return "yes" if ok else "**no**"


def _next(s: Spell) -> str:
    return "-" if s.next is None else f"`{s.next.value}`"


def kept_reading(path: Path) -> str:
    """The hand-written part of an existing report: from its `## Reading` heading on."""
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    at = text.find(f"\n{READING}")
    return text[at + 1 :] if at >= 0 else ""


def render_comparisons(summaries: Sequence[Summary]) -> list[str]:
    columns = (
        [SHORT[g] for g in ORDER]
        + ["Switches/yr (max)", "Max/yr besides failed FTDs", "False CORR"]
        + [f"CU in {w.name}" for w in UPTRENDS]
        + [f"CORR in {d.name} (SPY)" for d in DECLINES]
        + ["2020: EU / CU after", "Failed EU"]
    )
    lines = ["| Variant | " + " | ".join(columns) + " |", "|---|" + "---|" * len(columns)]
    for s in summaries:
        cells = [_pct(s.shares.get(g), 0) for g in ORDER]
        cells.append(f"{s.mean_switches:.1f} ({s.max_switches})")
        cells.append(str(s.max_other_switches))
        cells.append(str(len(s.false)))
        cells += [_pct(s.uptrends[w.name][Regime.CONFIRMED_UPTREND], 0) for w in UPTRENDS]
        cells += [
            "-" if d.correction is None else f"{d.correction} ({_signed(d.correction_drawdown)})"
            for d in s.declines
        ]
        cells.append(f"{s.recovery.early or '-'} / {s.recovery.confirmed or '-'}")
        cells.append(f"{len(s.failed_early)} of {len(s.early)}")
        lines.append(f"| `{s.name}` | " + " | ".join(cells) + " |")
    names = list(summaries[0].checks())
    lines += ["", "| Variant | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for s in summaries:
        lines.append(f"| `{s.name}` | " + " | ".join(_yes(v) for v in s.checks().values()) + " |")
    lines += ["", "Variants:"] + [f"- `{s.name}`: {s.description}." for s in summaries]
    return lines


def _forward_cell(f: Forward | None) -> str:
    if f is None:
        return "-"
    return (
        f"{_signed(f.mean)} / {_signed(f.median)} / {_pct(f.positive, 0)} / "
        f"{_signed(f.mean_drawdown)} (n {f.n})"
    )


def render_detail(
    name: str, results: Sequence[RegimeResult], spy: Spy, summary: Summary
) -> list[str]:
    shown = reported(results)
    n = len(shown)
    lines = [f"### `{name}` in detail", "", f"![Regime timeline over SPY]({CHART.name})", ""]

    lines += ["**Sessions per regime and year**, with regime switches:", ""]
    lines += ["| Year | " + " | ".join(SHORT[g] for g in ORDER) + " | Switches | Of failed FTDs |"]
    lines += ["|---|" + "---|" * (len(ORDER) + 2)]
    for year, counts in sorted(per_year(shown).items()):
        cells = [str(counts.get(g, 0)) for g in ORDER]
        cells += [str(summary.switches.get(year, 0)), str(summary.failed_ftd.get(year, 0))]
        lines.append(f"| {year} | " + " | ".join(cells) + " |")

    moves = ", ".join(
        f"{SHORT[a]} → {SHORT[b]} {k}" for (a, b), k in transitions(shown).most_common()
    )
    lines += ["", f"**Switches by kind:** {moves}."]

    lines += [
        "",
        "**SPY after each label** (price return from the label's close: mean / median / share "
        "positive / mean worst close over the horizon):",
        "",
        "| Regime | " + " | ".join(f"{h} sessions" for h in HORIZONS) + " |",
        "|---|" + "---|" * len(HORIZONS),
    ]
    stats = {h: forward_stats(shown, spy, h) for h in HORIZONS}
    for g in ORDER:
        lines.append(
            f"| {SHORT[g]} | " + " | ".join(_forward_cell(stats[h][g]) for h in HORIZONS) + " |"
        )

    lines += ["", "**Pillar scores** (share of sessions):", ""]
    lines += ["| Pillar | -1 | 0 | +1 |", "|---|---|---|---|"]
    for pillar, by_score in pillar_counts(shown).items():
        cells = [_pct(by_score[v] / n, 0) for v in (-1, 0, 1)]
        lines.append(f"| {pillar} | " + " | ".join(cells) + " |")
    scores = range(-4, 5)
    totals = score_counts(shown)
    lines += ["", "**Total score** (sessions), overall and inside each uptrend window:", ""]
    lines += ["| | " + " | ".join(f"{v:+d}" for v in scores) + " |"]
    lines += ["|---|" + "---|" * len(scores)]
    lines.append("| all | " + " | ".join(str(totals[v]) for v in scores) + " |")
    for w in UPTRENDS:
        c = window_scores(shown, w)
        lines.append(f"| {w.name} | " + " | ".join(str(c[v]) for v in scores) + " |")

    lines += ["", "**Uptrend windows** (share of sessions):", ""]
    lines += ["| Window | " + " | ".join(SHORT[g] for g in ORDER) + " |"]
    lines += ["|---|" + "---|" * len(ORDER)]
    for w in UPTRENDS:
        u = summary.uptrends[w.name]
        cells = [_pct(u[g], 0) for g in ORDER]
        lines.append(f"| {w.name} ({w.start} to {w.end}) | " + " | ".join(cells) + " |")

    lines += ["", "**Declines** (SPY's drawdown from the peak close at the first such label):", ""]
    lines += ["| Decline | Peak → low | First below CU | First CORRECTION | Early |"]
    lines += ["|---|---|---|---|---|"]
    for d in summary.declines:
        lines.append(
            f"| {d.decline.name} | {d.decline.start} → {d.decline.end} "
            f"({_signed(d.full_drawdown)}) | {d.defensive or '-'} "
            f"({_signed(d.defensive_drawdown)}) | {d.correction or '-'} "
            f"({_signed(d.correction_drawdown)}) | {_yes(d.early)} |"
        )
    r = summary.recovery
    lines += [
        "",
        f"**2020 recovery:** the first label above `CORRECTION` came {r.early} sessions after "
        f"the {RECOVERY_LOW} low (SPY {_signed(r.early_gain)} from the low), the first "
        f"`CONFIRMED_UPTREND` after {r.confirmed} (SPY {_signed(r.confirmed_gain)}).",
    ]

    lines += [
        "",
        "**False `CORRECTION` spells** (more than 15 sessions, SPY never 8% below its prior "
        "52-week high):",
        "",
    ]
    if summary.false:
        lines += ["| From | To | Sessions | Deepest close vs the 52-week high | Then |"]
        lines += ["|---|---|---|---|---|"]
        for s, worst in summary.false:
            lines.append(f"| {s.first} | {s.last} | {s.sessions} | {_signed(worst)} | {_next(s)} |")
    else:
        lines.append("None.")

    lines += ["", "**`EARLY_UPTREND` spells** (SPY over the sessions each one governed):", ""]
    lines += ["| From | To | Sessions | `no_ftd` | Then | SPY |", "|---|---|---|---|---|---|"]
    for e in summary.early:
        s = e.spell
        lines.append(
            f"| {s.first} | {s.last} | {s.sessions} | {'yes' if s.no_ftd else ''} | "
            f"{_next(s)} | {_signed(e.spy_return)} |"
        )
    crisis = sum(1 for x in shown if x.crisis.active)
    lines += ["", f"The crisis override was active on {crisis} of {n} sessions."]
    return lines


def render(
    summaries: Sequence[Summary],
    baseline: Sequence[RegimeResult],
    loaded: Loaded,
    risk: RiskConfig,
    seconds: float,
    reading: str = "",
) -> str:
    sessions = loaded.data.sessions
    shown = reported(baseline)
    windows = ", ".join(f"{w.name} ({w.start} to {w.end})" for w in UPTRENDS)
    lines = [
        "# Regime backtest (task 2.4)",
        "",
        f"**Run:** {datetime.now(UTC):%Y-%m-%d %H:%M} UTC with `uv run python "
        f"backtest/regime.py`, risk.yaml `{risk.short_hash}`, {seconds:.0f} s. Generated; only "
        "the Reading at the end is written by hand.",
        "",
        "## Setup",
        "",
        f"- **The tuning period only:** sessions {sessions[0]} to {sessions[-1]} "
        f"({len(sessions)}). Nothing after {TUNING_END} is loaded; the holdout (2023 on) is "
        "opened once, for the 5-pillar model, in the 3.8 re-run (README §15).",
        "- **Warm-up:** the machine starts in `CORRECTION` on the second session, as a replay "
        "does; 2016 fills the 200-day and 252-session windows, so results are reported from "
        f"{shown[0].session} ({len(shown)} sessions).",
        "- **4 pillars:** leadership health needs the breakout tracker (3.7) and scores 0 "
        "throughout, so the score runs from -4 to +4.",
        f"- **Universe:** point in time (README §5.1): {loaded.members} securities were in it at "
        f"some session, {loaded.delisted_members} of which no longer trade. Delisted names the "
        "vendor doesn't cover are missing; they were more often falling stocks, so breadth "
        "reads somewhat stronger than it was.",
        "- **SPY returns** are price returns from split-adjusted closes (no dividends).",
        "",
        "**Pre-registered** (set in the script before the first run on real data):",
        f"- declines: `CORRECTION` is reached while SPY's drawdown from the peak is under "
        f"{EARLY_SHARE:.0%} of the decline's full drawdown;",
        f"- the 2020 recovery: `EARLY_UPTREND` within {RECOVERY_EARLY_MAX} sessions of the "
        f"{RECOVERY_LOW} low and `CONFIRMED_UPTREND` within {RECOVERY_CONFIRMED_MAX};",
        f"- at most {MAX_SWITCHES_PER_YEAR} switches in any year, and at most "
        f"{MAX_FALSE_CORRECTIONS_PER_YEAR} false `CORRECTION` spell a year on average;",
        f"- **`CONFIRMED_UPTREND` covers at least {TARGET_CONFIRMED_SHARE:.0%}** of each strong "
        f"uptrend window: {windows}.",
        "",
        "## Comparisons",
        "",
        *render_comparisons(summaries),
        "",
        "## Baseline",
        "",
        *render_detail("baseline", baseline, loaded.spy, summaries[0]),
        "",
        reading.rstrip() if reading else f"{READING}\n\n(Not written yet.)",
        "",
    ]
    return "\n".join(lines)


COLORS = {
    Regime.CORRECTION: "#e8b4b4",
    Regime.EARLY_UPTREND: "#f3dca2",
    Regime.UNDER_PRESSURE: "#c9d6e8",
    Regime.CONFIRMED_UPTREND: "#b9dcb9",
}


def chart(results: Sequence[RegimeResult], spy: Spy, width: int = 1200, height: int = 320) -> str:
    """The regime as background bands under SPY's close on a log scale (an SVG)."""
    shown = reported(results)
    days = [r.session for r in shown]
    closes = [spy.close(d) for d in days]
    lo, hi = math.log(min(closes)), math.log(max(closes))
    pad, plot_h = 24, height - 48
    step = (width - 2 * pad) / max(len(days) - 1, 1)
    position = {d: i for i, d in enumerate(days)}

    def x(i: int) -> float:
        return pad + i * step

    def y(c: float) -> float:
        return pad + plot_h * (1 - (math.log(c) - lo) / max(hi - lo, 1e-9))

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="sans-serif" font-size="11">',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
    ]
    for s in spells(shown):
        a, b = position[s.first], position[s.last]
        parts.append(
            f'<rect x="{x(a) - step / 2:.1f}" y="{pad}" width="{(b - a + 1) * step:.1f}" '
            f'height="{plot_h}" fill="{COLORS[s.regime]}"/>'
        )
    path = " ".join(f"{'M' if i == 0 else 'L'}{x(i):.1f},{y(c):.1f}" for i, c in enumerate(closes))
    parts.append(f'<path d="{path}" fill="none" stroke="#1f2937" stroke-width="1.2"/>')
    for i, d in enumerate(days):
        if i == 0 or d.year != days[i - 1].year:
            parts.append(
                f'<line x1="{x(i):.1f}" y1="{pad}" x2="{x(i):.1f}" y2="{pad + plot_h}" '
                'stroke="#6b7280" stroke-width="0.5"/>'
            )
            parts.append(
                f'<text x="{x(i) + 3:.1f}" y="{pad + plot_h + 14}" fill="#374151">{d.year}</text>'
            )
    lx = pad
    for g in ORDER:
        parts.append(f'<rect x="{lx}" y="4" width="12" height="12" fill="{COLORS[g]}"/>')
        parts.append(f'<text x="{lx + 16}" y="14" fill="#374151">{g.value}</text>')
        lx += 24 + 7 * len(g.value)
    parts.append(
        f'<text x="{width - pad}" y="14" text-anchor="end" fill="#374151">'
        "SPY close, log scale</text>"
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def write_detail(name: str, results: Sequence[RegimeResult], folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / f"{name}.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["session", "regime", "score", *PILLARS, "no_ftd", "crisis"])
        for r in results:
            pillars = [getattr(r.pillars, p) for p in PILLARS]
            w.writerow(
                [r.session, r.regime.value, r.score, *pillars, r.state.no_ftd, r.crisis.active]
            )


def main(argv: Sequence[str] | None = None) -> int:
    from auratrade.calendar.sessions import load_calendar
    from auratrade.db.engine import engine_from_settings
    from auratrade.jobs.wiring import load_runtime_settings
    from auratrade.storage import make_store

    parser = argparse.ArgumentParser(prog="backtest/regime.py")
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args(argv)
    clock = time.monotonic()
    settings = load_runtime_settings()
    risk = load_risk_config(settings.risk_config_path)
    engine = engine_from_settings(settings)
    try:
        with engine.connect() as conn:
            calendar = load_calendar(conn)
    finally:
        engine.dispose()
    days = calendar.sessions_between(max(TUNING_START, calendar.first or TUNING_START), TUNING_END)
    loaded = load(make_store(settings), days, risk)
    print(f"loaded: {time.monotonic() - clock:.0f} s", file=sys.stderr)
    inputs = build_inputs(loaded.data, loaded.data.sessions[1:], risk.regime, risk.universe)
    print(f"inputs: {time.monotonic() - clock:.0f} s", file=sys.stderr)
    summaries, baseline = [], []
    for name, (description, change) in VARIANTS.items():
        results = run(inputs, change(risk.regime), risk.data.macro_stale_sessions)
        write_detail(name, results, DETAIL)
        summaries.append(summarize(name, description, results, loaded.spy))
        if name == "baseline":
            baseline = results
    CHART.write_text(chart(baseline, loaded.spy), encoding="utf-8")
    seconds = time.monotonic() - clock
    reading = kept_reading(args.report)
    args.report.write_text(
        render(summaries, baseline, loaded, risk, seconds, reading), encoding="utf-8"
    )
    print(f"wrote {args.report}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

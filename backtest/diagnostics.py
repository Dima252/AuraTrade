"""Why the baseline pool loses (task 3.8, after the gate failed): the splits fixed in
`docs/backtests/baseline-diagnostics-preregistration.md`, on the tuning period only.

    uv run python -m backtest.diagnostics    # -> docs/backtests/baseline-diagnostics.md

The reference pool is re-played as `backtest.baseline pool` plays it, then once more with every
slippage tier at 0 (D1). D9's charts go to ``var/backtests/diagnostics/`` for the eye check.
"""

from __future__ import annotations

import random
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime

from auratrade.charts import daily_chart
from auratrade.config import REPO_ROOT, RiskConfig, Strategy, load_risk_config
from auratrade.plans.model import Rule
from auratrade.sim.minutes import MinuteCache
from auratrade.sim.sessions import Minutes
from auratrade.sim.trade import FillKind
from backtest import regime as bt
from backtest.baseline import CHOSEN, Loaded, _load, pool_inputs, variant_named
from backtest.comparisons import MIN_CELL_TRADES, Difference, difference
from backtest.pool import (
    PoolInputs,
    PoolTrade,
    expectancy,
    prefetch,
    run_pool,
    scale,
    traded_symbols,
)
from backtest.pool_report import _r

REPORT = REPO_ROOT / "docs" / "backtests" / "baseline-diagnostics.md"
CHARTS = REPO_ROOT / "var" / "backtests" / "diagnostics"
YEARS = tuple(range(2017, 2023))
MIN_YEAR_TRADES = 10
YEARS_NEEDED = 4
CHART_SEED = 38
CHARTS_PER_STRATEGY = 3
CHARTED = (
    Strategy.VCP_BREAKOUT,
    Strategy.MOMENTUM_BREAKOUT,
    Strategy.LEADER_PULLBACK,
    Strategy.EMERGING_LEADER,
)

type Key = Callable[[PoolTrade], str | None]


def _log(message: str) -> None:
    print(message, file=sys.stderr)


def zero_cost(risk: RiskConfig) -> RiskConfig:
    """``risk`` with every slippage tier at 0 (D1)."""
    tiers = [t.model_copy(update={"fraction": 0.0}) for t in risk.sim.slippage]
    return risk.model_copy(update={"sim": risk.sim.model_copy(update={"slippage": tiers})})


# --- Per-trade facts --------------------------------------------------------------------------


def entry_fill(t: PoolTrade) -> float | None:
    if t.result is None:
        return None
    return next((f.price for f in t.result.fills if f.kind is FillKind.ENTRY), None)


def chase(t: PoolTrade) -> float | None:
    """Entry fill / code pivot - 1 (both in the entry session's traded units)."""
    fill = entry_fill(t)
    if fill is None or t.approved is None or t.approved.pivot <= 0:
        return None
    return fill / t.approved.pivot - 1


def stop_width(t: PoolTrade) -> float | None:
    """(entry fill - initial stop) / entry fill."""
    fill = entry_fill(t)
    if fill is None or t.result is None or t.result.order is None:
        return None
    return (fill - t.result.order.stop) / fill


def exit_session(t: PoolTrade) -> int | None:
    if t.result is None or not t.result.fills:
        return None
    return t.result.fills[-1].session


def bucket(value: float | None, edges: Sequence[float], labels: Sequence[str]) -> str | None:
    """``labels[i]`` for the first edge ``value`` is below; the last label above them all."""
    if value is None:
        return None
    for edge, label in zip(edges, labels, strict=False):
        if value < edge:
            return label
    return labels[-1]


@dataclass(frozen=True, slots=True)
class Split:
    number: str
    name: str
    labels: tuple[str, ...]
    key: Key


def splits(p: PoolInputs) -> list[Split]:
    """D2-D7 with the buckets of the pre-registration."""

    def regime_score(t: PoolTrade) -> int | None:
        evening = p.previous_session(t.entry_day)
        r = p.results.get(evening) if evening is not None else None
        return None if r is None else r.score

    def pillar(name: str) -> Callable[[PoolTrade], int | None]:
        def of(t: PoolTrade) -> int | None:
            evening = p.previous_session(t.entry_day)
            r = p.results.get(evening) if evening is not None else None
            return None if r is None else int(getattr(r.pillars, name))

        return of

    def feature(name: str) -> Callable[[PoolTrade], float | None]:
        def of(t: PoolTrade) -> float | None:
            f = t.listing.found.features
            value = None if f is None else getattr(f, name)
            return None if value is None else float(value)

        return of

    def dollar_volume(t: PoolTrade) -> float | None:
        return p.dollar_volume_at(t.listing.found.security, t.listing.found.index)

    def score_bucket(t: PoolTrade) -> str | None:
        s = regime_score(t)
        return (
            None
            if s is None
            else "<= +2"
            if s <= 2
            else "+3"
            if s == 3
            else "+4"
            if s == 4
            else ">= +5"
        )

    def eps(t: PoolTrade) -> str:
        g = feature("eps_growth")(t)
        return "none or <= 0" if g is None or g <= 0 else "0-25%" if g <= 0.25 else "> 25%"

    def stage(t: PoolTrade) -> str | None:
        s = feature("base_stage")(t)
        return None if s is None else "1" if s <= 1 else "2" if s <= 2 else "3+"

    def sign(name: str, low: str, high: str, *, at: int = 0) -> Key:
        def of(t: PoolTrade) -> str | None:
            v = pillar(name)(t)
            return None if v is None else high if v > at else low

        return of

    m = 1e6
    return [
        Split(
            "D2",
            "chase above the pivot",
            ("<= 0.5%", "0.5-1.5%", "1.5-3%", "> 3%"),
            lambda t: bucket(
                chase(t), (0.005, 0.015, 0.03), ("<= 0.5%", "0.5-1.5%", "1.5-3%", "> 3%")
            ),
        ),
        Split(
            "D3",
            "initial stop width",
            ("< 3%", "3-5%", "5-7%", ">= 7%"),
            lambda t: bucket(stop_width(t), (0.03, 0.05, 0.07), ("< 3%", "3-5%", "5-7%", ">= 7%")),
        ),
        Split(
            "D4",
            "rank in the capped list",
            ("1-10", "11-20", "21-40"),
            lambda t: bucket(t.listing.rank, (11, 21), ("1-10", "11-20", "21-40")),
        ),
        Split(
            "D4",
            "conviction",
            ("top quarter", "the rest"),
            lambda t: "top quarter" if t.listing.conviction >= 4 else "the rest",
        ),
        Split(
            "D5", "regime score the evening before", ("<= +2", "+3", "+4", ">= +5"), score_bucket
        ),
        Split("D5", "leadership pillar", ("<= 0", "> 0"), sign("leadership", "<= 0", "> 0")),
        Split("D5", "distribution pillar", ("< 0", "0"), sign("distribution", "< 0", "0", at=-1)),
        Split(
            "D6",
            "50-session dollar volume",
            ("< $25M", "$25-100M", "$100-500M", ">= $500M"),
            lambda t: bucket(
                dollar_volume(t),
                (25 * m, 100 * m, 500 * m),
                ("< $25M", "$25-100M", "$100-500M", ">= $500M"),
            ),
        ),
        Split(
            "D6",
            "entry price",
            ("< $10", "$10-20", "$20-50", ">= $50"),
            lambda t: bucket(entry_fill(t), (10, 20, 50), ("< $10", "$10-20", "$20-50", ">= $50")),
        ),
        Split(
            "D7",
            "RS rating",
            ("< 80", "80-90", ">= 90"),
            lambda t: bucket(feature("rs_rating")(t), (80, 90), ("< 80", "80-90", ">= 90")),
        ),
        Split(
            "D7",
            "group RS",
            ("< 50", "50-80", ">= 80"),
            lambda t: bucket(feature("group_rs")(t), (50, 80), ("< 50", "50-80", ">= 80")),
        ),
        Split(
            "D7",
            "extension above the 50-day",
            ("< 5%", "5-15%", "15-25%", ">= 25%"),
            lambda t: bucket(
                feature("extension")(t), (0.05, 0.15, 0.25), ("< 5%", "5-15%", "15-25%", ">= 25%")
            ),
        ),
        Split(
            "D7",
            "ADR",
            ("< 3%", "3-5%", "5-8%", ">= 8%"),
            lambda t: bucket(
                feature("adr")(t), (0.03, 0.05, 0.08), ("< 3%", "3-5%", "5-8%", ">= 8%")
            ),
        ),
        Split("D7", "EPS growth", ("none or <= 0", "0-25%", "> 25%"), eps),
        Split("D7", "base stage", ("1", "2", "3+"), stage),
    ]


# --- Reading a split --------------------------------------------------------------------------


def closed(trades: Sequence[PoolTrade]) -> list[PoolTrade]:
    return [t for t in trades if t.r is not None]


def mean_r(trades: Sequence[PoolTrade]) -> float | None:
    rs = [t.r for t in trades if t.r is not None]
    return statistics.fmean(rs) if rs else None


@dataclass(frozen=True, slots=True)
class Reading:
    """The best bucket against the worst, by the pre-registration's rule 2."""

    best: str
    worst: str
    diff: Difference
    years_won: int
    years_counted: int

    @property
    def informative(self) -> bool:
        return self.diff.beyond_noise and self.years_won >= YEARS_NEEDED


def read_split(groups: Mapping[str, Sequence[PoolTrade]]) -> Reading | None:
    """Best vs worst among the buckets of 30 trades or more."""
    sized = {k: v for k, v in groups.items() if len(v) >= MIN_CELL_TRADES}
    if len(sized) < 2:
        return None
    ranked = sorted(sized, key=lambda k: mean_r(sized[k]) or 0.0)
    worst, best = ranked[0], ranked[-1]
    won = counted = 0
    for year in YEARS:
        b = [t for t in sized[best] if t.entry_day.year == year]
        w = [t for t in sized[worst] if t.entry_day.year == year]
        if len(b) >= MIN_YEAR_TRADES and len(w) >= MIN_YEAR_TRADES:
            counted += 1
            won += (mean_r(b) or 0.0) > (mean_r(w) or 0.0)
    return Reading(best, worst, difference(sized[worst], sized[best], None), won, counted)


def group(trades: Sequence[PoolTrade], key: Key) -> dict[str, list[PoolTrade]]:
    out: dict[str, list[PoolTrade]] = defaultdict(list)
    for t in trades:
        k = key(t)
        if k is not None:
            out[k].append(t)
    return out


# --- The report -------------------------------------------------------------------------------


def _cell(trades: Sequence[PoolTrade]) -> str:
    return f"{_r(mean_r(trades))} ({len(trades)})" if trades else "-"


def _win(trades: Sequence[PoolTrade]) -> str:
    rs = [t.r for t in trades if t.r is not None]
    return f"{sum(r > 0 for r in rs) / len(rs):.0%}" if rs else "-"


def _reading_line(name: str, reading: Reading | None) -> str:
    if reading is None:
        return f"{name}: fewer than two buckets of {MIN_CELL_TRADES} trades."
    i = reading.diff.interval
    span = "-" if i is None else f"{_r(i.low)} to {_r(i.high)}"
    verdict = "**informative**" if reading.informative else "not informative"
    return (
        f"{name}: best `{reading.best}` vs worst `{reading.worst}` {_r(reading.diff.difference)} "
        f"({span}); best ahead in {reading.years_won} of {reading.years_counted} years "
        f"-> {verdict}."
    )


def render_split(split: Split, trades: Sequence[PoolTrade]) -> tuple[list[str], list[str]]:
    """The split's tables and the names of its informative readings."""
    groups = group(trades, split.key)
    labels = [k for k in split.labels if k in groups]
    lines = [
        f"### {split.number}: {split.name}",
        "",
        "| Bucket | Trades | Expectancy (R) | Win rate | " + " | ".join(map(str, YEARS)) + " |",
        "|---|---|---|---|" + "---|" * len(YEARS),
    ]
    for k in labels:
        g = groups[k]
        years = [_cell([t for t in g if t.entry_day.year == y]) for y in YEARS]
        lines.append(
            f"| `{k}` | {len(g)} | {_r(mean_r(g))} | {_win(g)} | " + " | ".join(years) + " |"
        )
    lines += ["", "| Strategy | " + " | ".join(f"`{k}`" for k in labels) + " |"]
    lines.append("|---|" + "---|" * len(labels))
    by_strategy = {s: [t for t in trades if t.strategy is s] for s in Strategy}
    for s, ts in by_strategy.items():
        sg = group(ts, split.key)
        lines.append(f"| `{s.value}` | " + " | ".join(_cell(sg.get(k, [])) for k in labels) + " |")
    lines.append("")
    found = []
    reading = read_split(groups)
    lines.append("- " + _reading_line("All", reading))
    if reading is not None and reading.informative:
        found.append(f"{split.number} {split.name} (all)")
    for s, ts in by_strategy.items():
        r = read_split(group(ts, split.key))
        if r is not None:
            lines.append("- " + _reading_line(f"`{s.value}`", r))
            if r.informative:
                found.append(f"{split.number} {split.name} (`{s.value}`)")
    return [*lines, ""], found


def render_costs(reference: Sequence[PoolTrade], free: Sequence[PoolTrade]) -> list[str]:
    lines = [
        "## D1: costs",
        "",
        "The pool re-played through `sim/` with every slippage tier at 0, against the reference.",
        "",
        "| Strategy | Reference (R) | Zero cost (R) | Difference | 90% interval "
        "| Zero-cost gate interval |",
        "|---|---|---|---|---|---|",
    ]
    for s in (None, *Strategy):
        a = [t for t in reference if s is None or t.strategy is s]
        b = [t for t in free if s is None or t.strategy is s]
        d = difference(a, b, s)
        e = expectancy(b)
        di = "-" if d.interval is None else f"{_r(d.interval.low)} to {_r(d.interval.high)}"
        gi = "-" if e.interval is None else f"{_r(e.interval.low)} to {_r(e.interval.high)}"
        name = "**All**" if s is None else f"`{s.value}`"
        lines.append(
            f"| {name} | {_r(d.reference)} ({d.reference_trades}) | {_r(d.variant)} "
            f"({d.variant_trades}) | {_r(d.difference)} | {di} | {gi} |"
        )
    return [*lines, ""]


def render_anatomy(trades: Sequence[PoolTrade]) -> list[str]:
    lines = ["## D8: anatomy of the losses", "", "### The exit rule", ""]
    lines += ["| Exit | Trades | Share | Mean R |", "|---|---|---|---|"]
    by_exit = group(
        trades,
        lambda t: "none" if t.result is None or t.result.exit is None else t.result.exit.value,
    )
    for k, g in sorted(by_exit.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"| `{k}` | {len(g)} | {len(g) / len(trades):.0%} | {_r(mean_r(g))} |")
    lines += ["", "### The exit session", "", "| Session | Trades | Mean R |", "|---|---|---|"]
    by_session = group(
        trades, lambda t: bucket(exit_session(t), (2, 6, 21), ("1", "2-5", "6-20", "> 20"))
    )
    for k in ("1", "2-5", "6-20", "> 20"):
        g = by_session.get(k, [])
        lines.append(f"| {k} | {len(g)} | {_r(mean_r(g))} |")
    lines += [
        "",
        "### Failed breakouts and given-back gains",
        "",
        "| Strategy | Trades | Stopped out at a loss within 5 sessions "
        "| Losers that closed at +1R first | Mean of the best close (R) |",
        "|---|---|---|---|---|",
    ]
    for s in (None, *Strategy):
        ts = [t for t in trades if s is None or t.strategy is s]
        if not ts:
            continue
        failed = sum(
            1
            for t in ts
            if t.result is not None
            and t.result.exit is Rule.HARD_STOP
            and (t.r or 0) < 0
            and (exit_session(t) or 99) <= 5
        )
        losers = [t for t in ts if (t.r or 0) <= 0 and t.result is not None]
        gave_back = sum(
            1 for t in losers if t.result is not None and max(t.result.marks, default=0) >= 1
        )
        best = [max(t.result.marks, default=0.0) for t in ts if t.result is not None]
        name = "**All**" if s is None else f"`{s.value}`"
        lines.append(
            f"| {name} | {len(ts)} | {failed / len(ts):.0%} | "
            f"{gave_back / len(losers) if losers else 0:.0%} of {len(losers)} | "
            f"{_r(statistics.fmean(best) if best else None)} |"
        )
    return [*lines, ""]


# --- D9: the charts ---------------------------------------------------------------------------


def chart_sample(trades: Sequence[PoolTrade]) -> list[PoolTrade]:
    rng = random.Random(CHART_SEED)  # noqa: S311 (a reproducible sample, not a secret)
    out = []
    for s in CHARTED:
        ts = sorted(
            (t for t in trades if t.strategy is s),
            key=lambda t: (t.entry_day, t.listing.found.security),
        )
        out += rng.sample(ts, min(CHARTS_PER_STRATEGY, len(ts)))
    return out


def draw(p: PoolInputs, risk: RiskConfig, trades: Sequence[PoolTrade]) -> list[str]:
    """Each sampled trade's daily chart up to 20 sessions after the entry, in adjusted units."""
    CHARTS.mkdir(parents=True, exist_ok=True)
    lines = [
        "## D9: the charts",
        "",
        f"Drawn at random (seed {CHART_SEED}) and read by eye; the files are in "
        "`var/backtests/diagnostics/`. Red: the pivot; grey: the initial stop.",
        "",
        "| File | Strategy | Scan session | Entry | R | Exit |",
        "|---|---|---|---|---|---|",
    ]
    for t in chart_sample(trades):
        f = t.listing.found
        s = p.h.series[f.security]
        k = scale(p.h, f.security, t.entry_day)
        end = min(f.index + 21, len(s.dates))
        stop = None if t.result is None or t.result.order is None else t.result.order.stop / k
        png = daily_chart(
            f.setup.symbol,
            s.dates[:end],
            s.bars()[:end],
            risk.charts,
            pivot=f.setup.pivot,
            stop=stop,
        )
        name = f"{t.strategy.value.lower()}-{f.setup.symbol}-{t.listing.scan_day}.png"
        (CHARTS / name).write_bytes(png)
        exit_rule = "-" if t.result is None or t.result.exit is None else t.result.exit.value
        lines.append(
            f"| {name} | `{t.strategy.value}` | {t.listing.scan_day} | {t.entry_day} | "
            f"{_r(t.r)} | `{exit_rule}` |"
        )
    return [*lines, ""]


def render(
    p: PoolInputs,
    risk: RiskConfig,
    reference: Sequence[PoolTrade],
    free: Sequence[PoolTrade],
    seconds: float,
    reading: str,
) -> str:
    trades = closed(reference)
    body: list[str] = []
    informative: list[str] = []
    for split in splits(p):
        lines, found = render_split(split, trades)
        body += lines
        informative += found
    lines = [
        "# Why the baseline pool loses (task 3.8 diagnostics)",
        "",
        f"**Run:** {datetime.now(UTC):%Y-%m-%d %H:%M} UTC with `uv run python -m "
        f"backtest.diagnostics`, risk.yaml `{risk.short_hash}`, regime `{CHOSEN}`, "
        f"{seconds:.0f} s. "
        "Generated; only the Reading at the end is written by hand. What is computed and how it "
        "is read were fixed first: docs/backtests/baseline-diagnostics-preregistration.md. "
        "**Tuning period only (2017-2022)**; the holdout stays closed.",
        "",
        f"Closed trades in the reference pool: {len(trades):,}, expectancy {_r(mean_r(trades))}.",
        "",
        *render_costs(reference, free),
        "## D2-D7: the splits",
        "",
        "Each split gives the buckets over all strategies, each strategy's expectancy by bucket, "
        "and the reading of rule 2: the best bucket against the worst (buckets of 30 trades or "
        f"more), its 90% interval, and the years the best was ahead (of those with "
        f"{MIN_YEAR_TRADES}+ trades in both). Informative = the interval excludes zero and the "
        f"best was ahead in {YEARS_NEEDED}+ years.",
        "",
        "**Informative:** " + ("; ".join(informative) if informative else "none") + ".",
        "",
        *body,
        *render_anatomy(trades),
        *draw(p, risk, trades),
        reading.rstrip() if reading else f"{bt.READING}\n\n(Not written yet.)",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    from auratrade.jobs.simulate import _alpaca
    from auratrade.jobs.wiring import load_runtime_settings

    clock = time.monotonic()
    risk = load_risk_config(load_runtime_settings().risk_config_path)
    loaded: Loaded = _load(risk)
    p, lists = pool_inputs(loaded, risk, variant_named(CHOSEN))
    cache = MinuteCache(loaded.store, _alpaca(loaded.settings))
    held: dict[date, dict[str, Minutes]] = {}

    def symbols_for(wanted: Mapping[date, set[str]]) -> Mapping[tuple[date, str], str]:
        return traded_symbols(loaded.store, wanted)

    def minutes_for(wanted: Mapping[date, set[str]]) -> Mapping[date, Mapping[str, Minutes]]:
        missing = {d: s - held.get(d, {}).keys() for d, s in wanted.items()}
        missing = {d: s for d, s in missing.items() if s}
        if missing:
            for d, got in prefetch(p, cache, missing, log=_log).items():
                held.setdefault(d, {}).update(got)
        return {d: {x: held[d][x] for x in s if x in held.get(d, {})} for d, s in wanted.items()}

    reference = run_pool(p, lists, minutes_for, symbols_for)
    _log(f"reference: {time.monotonic() - clock:.0f} s")
    free = run_pool(replace(p, risk=zero_cost(risk)), lists, minutes_for, symbols_for)
    _log(f"zero cost: {time.monotonic() - clock:.0f} s")
    reading = bt.kept_reading(REPORT)
    REPORT.write_text(
        render(p, risk, reference, free, time.monotonic() - clock, reading), encoding="utf-8"
    )
    _log(f"wrote {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

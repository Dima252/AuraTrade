"""The setup pool's part of `docs/backtests/baseline.md` (3.8 step 4): the baseline gate on the
tuning period and the pool's diagnostics. Generated; the Reading at the end is kept."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from auratrade.config import Regime, RiskConfig, Strategy
from auratrade.guardrails.model import GuardrailRule, Outcome
from backtest.comparisons import Difference, Variant, depth_buckets
from backtest.pool import PoolTrade, Status, expectancy, rejections

READING = "## Reading"
ORDER = (
    Strategy.VCP_BREAKOUT,
    Strategy.MOMENTUM_BREAKOUT,
    Strategy.EPISODIC_PIVOT,
    Strategy.LEADER_PULLBACK,
    Strategy.EMERGING_LEADER,
)


def _r(x: float | None, digits: int = 2) -> str:
    return "-" if x is None else f"{x:+.{digits}f}"


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.0%}"


def _gate_row(name: str, trades: Iterable[PoolTrade]) -> str:
    e = expectancy(trades)
    interval = "-" if e.interval is None else f"{e.interval.low:+.2f} to {e.interval.high:+.2f}"
    verdict = "**passes**" if e.passes else "fails"
    return (
        f"| {name} | {e.trades:,} | {e.sessions:,} | {_r(e.mean)} | {interval} | {verdict} | "
        f"{_pct(e.win_rate)} | {_r(e.avg_gain)} | {_r(e.avg_loss)} |"
    )


def render_pool(
    trades: Sequence[PoolTrade],
    risk: RiskConfig,
    variant: str,
    seconds: float,
    reading: str = "",
    *,
    survivors: Sequence[PoolTrade] = (),
    compared: Sequence[tuple[Variant, Sequence[Difference]]] = (),
    notes: Sequence[str] = (),
) -> str:
    by: dict[Strategy, list[PoolTrade]] = defaultdict(list)
    for t in trades:
        by[t.strategy].append(t)
    years = sorted({t.entry_day.year for t in trades})
    statuses = list(Status)
    lines = [
        "# Baseline backtest (task 3.8)",
        "",
        f"**Run:** {datetime.now(UTC):%Y-%m-%d %H:%M} UTC with `uv run python -m "
        f"backtest.baseline pool`, risk.yaml `{risk.short_hash}`, regime `{variant}` "
        f"(docs/backtests/regime-5-pillar.md), {seconds:.0f} s. Generated; only the Reading at "
        "the end is written by hand. Method: docs/backtests/baseline-preregistration.md.",
        "",
        "**The tuning period only (2017-2022).** The holdout (2023 on) is not loaded. The gate's "
        "second half, a positive holdout estimate, is checked once, after the regime and the "
        "defaults are frozen (step 5).",
        "",
        "## The pool",
        "",
        "Every unit with a rank in a session's capped list, attempted on each following session "
        "while listed, until it fills (`backtest/pool.py`). Attempts by outcome:",
        "",
        "| Strategy | " + " | ".join(s.value for s in statuses) + " |",
        "|---|" + "---|" * len(statuses),
    ]
    for strategy in ORDER:
        counts = Counter(t.status for t in by[strategy])
        lines.append(
            f"| `{strategy.value}` | " + " | ".join(f"{counts[s]:,}" for s in statuses) + " |"
        )
    if notes:
        lines += ["", *notes]
    lines += [
        "",
        "## The baseline gate (tuning period)",
        "",
        "Closed trades only (a trade still open on 2022-12-30 is reported apart). R is the P&L "
        "over the dollars put at risk; the interval resamples whole entry sessions (2,000 draws, "
        "seed 38, 90%, nearest rank). **Pass:** the interval's low end above zero.",
        "",
        "| Pool | Trades | Sessions | Expectancy (R) | 90% interval | Tuning half | Win rate | "
        "Avg gain (R) | Avg loss (R) |",
        "|---|---|---|---|---|---|---|---|---|",
        _gate_row("**All**", trades),
        *(_gate_row(f"`{s.value}`", by[s]) for s in ORDER),
        "",
        "**Survivors only** (the live rule: a security without report dates is rejected by "
        "the earnings blackout, so almost every delisted name is out): "
        + _survivors(survivors)
        + ".",
        "",
        "Open at the end: "
        + ", ".join(f"`{s.value}` {sum(t.status is Status.OPEN for t in by[s])}" for s in ORDER)
        + ".",
        "",
        "## Per year",
        "",
        "Expectancy (R) and closed trades by entry year.",
        "",
        "| Strategy | " + " | ".join(str(y) for y in years) + " |",
        "|---|" + "---|" * len(years),
    ]
    for name, group in [("**All**", list(trades)), *((f"`{s.value}`", by[s]) for s in ORDER)]:
        cells = []
        for y in years:
            e = expectancy(t for t in group if t.entry_day.year == y)
            cells.append(f"{_r(e.mean)} ({e.trades})")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "Total R by year (all strategies): "
        + ", ".join(
            f"{y} {_r(sum(t.r or 0 for t in trades if t.entry_day.year == y), 1)}" for y in years
        )
        + ".",
        "",
        "## By regime",
        "",
        "Expectancy (R) and closed trades by the regime computed the evening before the entry.",
        "",
        "| Strategy | " + " | ".join(f"`{g.value}`" for g in reversed(Regime)) + " |",
        "|---|" + "---|" * len(Regime),
    ]
    for s in ORDER:
        cells = []
        for g in reversed(Regime):
            e = expectancy(t for t in by[s] if t.regime is g)
            cells.append(f"{_r(e.mean)} ({e.trades})")
        lines.append(f"| `{s.value}` | " + " | ".join(cells) + " |")
    lines += ["", *_comparisons(compared), "", *_depth(trades)]
    lines += ["", *_diagnostics(trades, by)]
    lines += ["", reading.rstrip() if reading else f"{READING}\n\n(Not written yet.)", ""]
    return "\n".join(lines)


def _diagnostics(trades: Sequence[PoolTrade], by: dict[Strategy, list[PoolTrade]]) -> list[str]:
    rules = rejections(trades)
    shown = sorted({r for counts in rules.values() for r in counts}, key=lambda r: r.value)
    lines = [
        "## Diagnostics",
        "",
        "### Guardrail rejections",
        "",
        "The rule that rejected each attempt (the first rejection ends the check), per strategy.",
        "",
        "| Strategy | " + " | ".join(f"`{r.value}`" for r in shown) + " |",
        "|---|" + "---|" * len(shown),
    ]
    for s in ORDER:
        lines.append(
            f"| `{s.value}` | " + " | ".join(f"{rules[s].get(r, 0):,}" for r in shown) + " |"
        )
    lines += [
        "",
        "### Whole shares at $5,000",
        "",
        "| Strategy | Rejected at 0 shares | One tranche instead of two | 09:36 skips |",
        "|---|---|---|---|",
    ]
    for s in ORDER:
        zero = sum(
            t.status is Status.REJECTED
            and any(
                f.rule is GuardrailRule.WHOLE_SHARES and f.outcome is Outcome.REJECT
                for f in t.findings
            )
            for t in by[s]
        )
        single = sum(
            t.approved is not None
            and any(
                f.rule is GuardrailRule.WHOLE_SHARES and f.outcome is Outcome.CLIP
                for f in t.findings
            )
            for t in by[s]
        )
        skips = Counter(
            f.rule.value
            for t in by[s]
            if t.status is Status.SKIPPED and t.result is not None
            for f in t.result.findings[-1:]
        )
        lines.append(
            f"| `{s.value}` | {zero:,} | {single:,} | "
            + (", ".join(f"{k} {n}" for k, n in skips.most_common()) or "0")
            + " |"
        )
    missed = Counter(t.strategy for t in trades if t.status is Status.MISSED)
    reached = Counter(
        t.strategy for t in trades if t.status in (Status.MISSED, Status.CLOSED, Status.OPEN)
    )
    lines += [
        "",
        "Placed at 09:36 but never filled (not triggered after the open, or past the limit): "
        + ", ".join(f"`{s.value}` {missed[s]} of {reached[s]}" for s in ORDER)
        + ".",
    ]
    return lines


def _survivors(trades: Sequence[PoolTrade]) -> str:
    if not trades:
        return "not run"
    e = expectancy(trades)
    interval = "-" if e.interval is None else f"{e.interval.low:+.2f} to {e.interval.high:+.2f}"
    return f"{_r(e.mean)} over {e.trades:,} trades ({interval})"


def _comparisons(compared: Sequence[tuple[Variant, Sequence[Difference]]]) -> list[str]:
    lines = [
        "## Comparisons (tuning period only)",
        "",
        "Each variant changes one dimension against the reference (the defaults; for exits each "
        "strategy's single-trader set) and re-plays the pool (`backtest/comparisons.py`). The "
        "difference is the variant's expectancy minus the reference's, on the strategies it "
        "changes; its 90% interval resamples whole entry sessions of both. **Beyond noise** = the "
        "interval excludes zero. Nothing is adopted here: adoption also needs the same sign on "
        "the holdout, which stays closed while the pool fails its gate. Cells under 30 trades "
        "keep the default.",
        "",
    ]
    if not compared:
        return [*lines, "(Not run.)"]
    lines += [
        "| # | Variant | Strategies | Reference (R) | Variant (R) | Difference | 90% interval "
        "| Beyond noise |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for v, diffs in compared:
        for d in diffs:
            who = "together" if d.strategy is None else f"`{d.strategy.value}`"
            if d.strategy is None and len(diffs) == 2:
                continue  # one strategy: its own row says it all
            interval = (
                "-" if d.interval is None else f"{d.interval.low:+.2f} to {d.interval.high:+.2f}"
            )
            noise = (
                "too few trades"
                if not d.enough
                else ("**better**" if (d.difference or 0) > 0 else "**worse**")
                if d.beyond_noise
                else "no"
            )
            lines.append(
                f"| {v.number} | {v.description} | {who} | {_r(d.reference)} "
                f"({d.reference_trades}) | {_r(d.variant)} ({d.variant_trades}) | "
                f"{_r(d.difference)} | {interval} | {noise} |"
            )
    lines += [
        "",
        "Not run: #1 (the code has no trigger that stays at the pivot on a gap open), #15 (the "
        'code exempts `MomentumBreakout` from the 50-day rule, so "with" needs a code switch), '
        "and the book-measured #9-#12, #14 and the book halves of #5 and #13 (the baseline "
        "book's portfolio simulation isn't built).",
    ]
    return lines


def _depth(trades: Sequence[PoolTrade]) -> list[str]:
    lines = [
        "### #19: momentum results by consolidation depth",
        "",
        "| Depth | Trades | Expectancy (R) |",
        "|---|---|---|",
    ]
    for bucket, group in depth_buckets(trades).items():
        e = expectancy(group)
        lines.append(f"| {bucket} | {e.trades} | {_r(e.mean)} |")
    return lines

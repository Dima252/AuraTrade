"""The setup pool's part of `docs/backtests/baseline.md` (3.8 step 4): the baseline gate on the
tuning period and the pool's diagnostics. Generated; the Reading at the end is kept."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from auratrade.config import Regime, RiskConfig, Strategy
from auratrade.guardrails.model import GuardrailRule, Outcome
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
    trades: Sequence[PoolTrade], risk: RiskConfig, variant: str, seconds: float, reading: str = ""
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

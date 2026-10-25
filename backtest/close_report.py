"""`docs/backtests/close-entry.md` (task 3.8c-d, #144): the close-entry candidates against the
pre-registered gate. Generated; the Reading at the end is kept.

**Blind** (pre-registration step 3) the report holds counts and invariants only: attempts by
outcome, the gate's pass rates and failing conditions, stop widths, sizes, the decision-price
checks. Nothing derived from an outcome (R, P&L, expectancy, win rate, exits) is written.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from auratrade.config import Regime, RiskConfig, Strategy
from auratrade.guardrails.model import GuardrailRule, Outcome
from auratrade.plans.model import Rule
from backtest.close_entry import (
    CANDIDATE_1,
    CANDIDATE_2,
    MAX_DISAGREEMENT,
    MIN_POSITIVE_YEARS,
    MIN_TRADES,
    PREREGISTRATION_COMMIT,
    TUNING_YEARS,
    CloseRun,
    Verdict,
    gated,
    verdict,
)
from backtest.comparisons import Difference, Variant
from backtest.pool import PoolTrade, Status, expectancy
from backtest.pool_report import ORDER, READING

GATE_FAILURES = (
    ("below the level", "below the level"),
    ("max chase", "max chase"),
    ("upper part", "range"),
    ("volume", "volume"),
    ("RS line is not", "RS line"),
    ("50-session low", "RS low"),
    ("tested moving average", "MA"),
)
WIDTH_BUCKETS = ((0.03, "< 3%"), (0.05, "3-5%"), (0.08, "5-8%"), (float("inf"), ">= 8%"))
REFERENCE_0936 = (
    ("`VCP_BREAKOUT`", "1,023", "-0.03", "-0.14 to +0.07"),
    ("`MOMENTUM_BREAKOUT`", "130", "-0.51", "-0.68 to -0.30"),
    ("`LEADER_PULLBACK`", "1,500", "-0.22", "-0.31 to -0.14"),
    ("`EMERGING_LEADER`", "754", "-0.21", "-0.28 to -0.14"),
    ("`EPISODIC_PIVOT` (after #143)", "625", "+0.21", "-0.09 to +0.55"),
    ("All (after #143)", "4,052", "-0.11", "-0.18 to -0.04"),
)


def _r(x: float | None, digits: int = 2) -> str:
    return "-" if x is None else f"{x:+.{digits}f}"


def _pct(x: float | None, digits: int = 0) -> str:
    return "-" if x is None else f"{x * 100:.{digits}f}%"


def _share(n: int, of: int) -> str:
    return f"{n:,} of {of:,}" + (f" ({n / of:.0%})" if of else "")


def _by_strategy(trades: Iterable[PoolTrade]) -> dict[Strategy, list[PoolTrade]]:
    by: dict[Strategy, list[PoolTrade]] = defaultdict(list)
    for t in trades:
        by[t.strategy].append(t)
    return by


def render_close(
    run: CloseRun, risk: RiskConfig, variant: str, seconds: float, reading: str = ""
) -> str:
    by = _by_strategy(run.reference)
    title = "blind mechanics check" if run.blind else "the scored run"
    lines = [
        f"# The close entry (task 3.8, #144): {title}",
        "",
        f"**Run:** {datetime.now(UTC):%Y-%m-%d %H:%M} UTC with `uv run python -m backtest.baseline "
        f"close{' --blind' if run.blind else ''}`, the `CLOSE` candidate's risk.yaml "
        f"`{risk.short_hash}`, regime `{variant}`, {seconds:.0f} s. **Pre-registration:** "
        f"`docs/backtests/close-entry-preregistration.md`, approved by the user 2026-10-25 in "
        f"commit `{PREREGISTRATION_COMMIT}`. Generated; only the Reading at the end is written "
        "by hand.",
        "",
        "**The tuning period only (2017-2022).** The holdout (2023 on) is not loaded.",
        "",
    ]
    if run.blind:
        lines += [
            "**Blind (step 3).** Counts and invariants only: no R, P&L, expectancy, win rate or "
            "exit is computed into this page and no trades are saved. It checks the code on real "
            "data before the one scored run (step 4).",
            "",
        ]
    lines += _attempts(by)
    lines += _invariants(run)
    lines += _gate_mechanics(by)
    lines += _stops(by)
    lines += _sizes(by)
    lines += _decision_checks(run)
    if not run.blind:
        lines += _gate(run, by)
        lines += _per_year(by)
        lines += _per_regime(by)
        lines += _comparisons(run.compared)
        lines += _scored_diagnostics(run, by)
    lines += _reference()
    lines += _biases()
    lines += ["", reading.rstrip() if reading else f"{READING}\n\n(Not written yet.)", ""]
    return "\n".join(lines)


# --- Blind sections ---------------------------------------------------------------------------


def _attempts(by: dict[Strategy, list[PoolTrade]]) -> list[str]:
    statuses = [
        Status.REJECTED,
        Status.NOT_CONFIRMED,
        Status.SKIPPED,
        Status.MISSED,
        Status.CLOSED,
        Status.OPEN,
    ]
    lines = [
        "## The attempts",
        "",
        "Every unit with a rank in a session's capped list, attempted on each following session "
        "while listed until it fills (an EP on its gap session). `REJECTED`: a setup-level "
        "guardrail at proposal. `NOT_CONFIRMED`: the gate failed at the decision time. "
        "`SKIPPED`: the decision-time order check rejected it. `CLOSED` and `OPEN` are fills "
        "(blind: shown together as filled).",
        "",
        "| Strategy | Attempts | REJECTED | NOT_CONFIRMED | SKIPPED | MISSED | Filled |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in ORDER:
        c = Counter(t.status for t in by[s])
        filled = c[Status.CLOSED] + c[Status.OPEN]
        lines.append(
            f"| `{s.value}` | {len(by[s]):,} | "
            + " | ".join(f"{c[st]:,}" for st in statuses[:4])
            + f" | {filled:,} |"
        )
    other = {t.status for ts in by.values() for t in ts} - {*statuses}
    if other:
        lines += ["", "Other statuses: " + ", ".join(sorted(o.value for o in other)) + "."]
    return [*lines, ""]


def _invariants(run: CloseRun) -> list[str]:
    from backtest.close_entry import invariant_violations

    bad = invariant_violations(run.reference)
    lines = [
        "## Invariants",
        "",
        "Every fill is one tranche of at least one share, between its stop and its limit, with "
        "its gate passed and dollars at risk, and nothing fills after its exit.",
        "",
    ]
    if not bad:
        return [*lines, "**All hold.**", ""]
    return [*lines, f"**{len(bad)} violations:**", "", *(f"- {b}" for b in bad[:30]), ""]


def _gate_mechanics(by: dict[Strategy, list[PoolTrade]]) -> list[str]:
    names = [short for _, short in GATE_FAILURES]
    lines = [
        "## The gate at the decision time",
        "",
        "Attempts that reached the gate (past the proposal guardrails), how many passed, and "
        "how often each condition failed (an attempt can fail several).",
        "",
        "| Strategy | Reached | Passed | " + " | ".join(names) + " |",
        "|---|---|---|" + "---|" * len(names),
    ]
    for s in ORDER:
        reached = gated(by[s])
        passed = sum(
            1
            for t in reached
            if t.result is not None and t.result.gate is not None and t.result.gate.passed
        )
        fails: Counter[str] = Counter()
        for t in reached:
            if t.result is None or t.result.gate is None:
                continue
            for f in t.result.gate.failures:
                for key, short in GATE_FAILURES:
                    if key in f:
                        fails[short] += 1
        lines.append(
            f"| `{s.value}` | {len(reached):,} | {_share(passed, len(reached))} | "
            + " | ".join(f"{fails[n]:,}" for n in names)
            + " |"
        )
    return [*lines, ""]


def _width(t: PoolTrade) -> float | None:
    if t.result is None or t.result.order is None:
        return None
    o = t.result.order
    return (o.limit - o.stop) / o.limit


def _stops(by: dict[Strategy, list[PoolTrade]]) -> list[str]:
    lines = [
        "## Stops",
        "",
        "Stop-width rejections at the decision time (of the attempts whose gate passed; the "
        "width is measured from the limit) and at proposal (only a structural stop is checked "
        "there); how often the trigger session's low, rather than the structural stop, set the "
        "stop; and the width of the stops placed.",
        "",
        "| Strategy | Width rejections at the decision time | At proposal | Set by the day's "
        "low | " + " | ".join(name for _, name in WIDTH_BUCKETS) + " | Median width |",
        "|---|---|---|---|" + "---|" * len(WIDTH_BUCKETS) + "---|",
    ]
    for s in ORDER:
        ts = by[s]
        at_close = sum(
            1
            for t in ts
            if t.status is Status.SKIPPED
            and t.result is not None
            and any(f.rule is GuardrailRule.STOP_WIDTH for f in t.result.findings)
        )
        reached = sum(
            1
            for t in ts
            if t.result is not None and t.result.gate is not None and t.result.gate.passed
        )
        proposal = sum(
            1
            for t in ts
            if t.status is Status.REJECTED
            and any(
                f.rule is GuardrailRule.STOP_WIDTH and f.outcome is Outcome.REJECT
                for f in t.findings
            )
        )
        placed = [t for t in ts if t.triggered]
        by_low = sum(
            1
            for t in placed
            if t.result is not None
            and t.result.order is not None
            and t.approved is not None
            and t.result.order.stop > t.approved.stop
        )
        widths = [w for t in placed if (w := _width(t)) is not None]
        buckets = Counter(next(n for top, n in WIDTH_BUCKETS if w < top) for w in widths)
        lines.append(
            f"| `{s.value}` | {_share(at_close, reached)} | {proposal:,} | "
            f"{_share(by_low, len(placed))} | "
            + " | ".join(f"{buckets[n]:,}" for _, n in WIDTH_BUCKETS)
            + f" | {_pct(statistics.median(widths) if widths else None, 1)} |"
        )
    return [*lines, ""]


def _sizes(by: dict[Strategy, list[PoolTrade]]) -> list[str]:
    lines = [
        "## Sizes and the pay-up",
        "",
        "Under one share at $5,000 per risk level (rejected at proposal or at the decision "
        "time), and the entry above the level: in % of the level and in R (the fill minus the "
        "level over the fill minus the stop), medians over the fills.",
        "",
        "| Strategy | Under one share (risk: count) | Pay-up (%) | Pay-up (R) |",
        "|---|---|---|---|",
    ]
    for s in ORDER:
        zero: Counter[str] = Counter()
        for t in by[s]:
            found = list(t.findings) + list(t.result.findings if t.result is not None else ())
            if any(
                f.rule is GuardrailRule.WHOLE_SHARES and f.outcome is Outcome.REJECT for f in found
            ):
                risk_level = f"{t.approved.risk:.2%}" if t.approved else "proposal"
                zero[risk_level] += 1
        pct, in_r = [], []
        for t in by[s]:
            if not t.triggered or t.result is None or t.result.plan is None:
                continue
            fill = t.result.fills[0].price
            pivot, stop = t.result.plan.pivot, t.result.plan.initial_stop
            pct.append((fill - pivot) / pivot)
            if fill > stop:
                in_r.append((fill - pivot) / (fill - stop))
        lines.append(
            f"| `{s.value}` | "
            + (", ".join(f"{k}: {n}" for k, n in sorted(zero.items())) or "0")
            + f" | {_pct(statistics.median(pct) if pct else None, 1)} | "
            f"{_r(statistics.median(in_r) if in_r else None)} |"
        )
    return [*lines, ""]


def _decision_checks(run: CloseRun) -> list[str]:
    d, pd = run.decision, run.positions
    gap = (
        f"mean {_pct(statistics.fmean(d.gaps), 2)}, 90th percentile "
        f"{_pct(sorted(d.gaps)[int(0.9 * (len(d.gaps) - 1))], 2)}"
        if d.gaps
        else "-"
    )
    verdict_text = (
        "**within the threshold**"
        if d.ok
        else "**over the threshold: nothing is adopted from this run until the user decides**"
    )
    return [
        "## The decision-price checks (section 6)",
        "",
        f"**The gate at 15:40 vs the close.** {d.sampled} attempts that reached the gate, "
        f"drawn at random (seed 38); {d.compared} compared ({d.no_minutes} without minute bars "
        "or a 15:40 price). The 15:40 gate reads the last SIP trade before 15:40 and the "
        "range so far; volume is the full day in both. "
        f"**Disagreement: {_share(d.disagree, d.compared)}** against a threshold of "
        f"{MAX_DISAGREEMENT:.0%}: {verdict_text}. Price difference on the attempts both "
        f"passed: {gap}.",
        "",
        f"**Held sessions (owed from 3.8).** {pd.sampled} held position-days drawn at random "
        f"(seed 38); {pd.compared} compared ({pd.no_minutes} without minute bars). A session "
        "disagrees when its plan events or its sales and exits (other than the hard stop) "
        "differ between the 15:40 price and the close: "
        f"**{_share(pd.disagree, pd.compared)}**. Reported, decides nothing.",
        "",
    ]


# --- Scored sections ----------------------------------------------------------------------------


def _gate_row(name: str, v: Verdict, decision_ok: bool) -> str:
    e = v.expectancy
    primary = "-" if e.interval is None else f"{e.interval.low:+.2f} to {e.interval.high:+.2f}"
    second = "-" if v.secondary is None else f"{v.secondary.low:+.2f} to {v.secondary.high:+.2f}"
    if v.passes(decision_ok):
        result = "**passes**"
    elif v.one_year:
        result = "passes on one year"
    else:
        result = "fails"
    return (
        f"| {name} | {e.trades:,} | {_r(e.mean)} | {primary} | {second} | "
        f"{v.positive_years} of {len(TUNING_YEARS)} | {_r(v.without_best)} | "
        f"{'yes' if v.enough else 'no'} | {result} | {_pct(e.win_rate)} | {_r(e.avg_gain)} | "
        f"{_r(e.avg_loss)} |"
    )


def _gate(run: CloseRun, by: dict[Strategy, list[PoolTrade]]) -> list[str]:
    ok = run.decision.ok
    verdicts = {s: verdict(by[s], run.block_of) for s in ORDER}
    passing = [s for s in ORDER if verdicts[s].passes(ok)]
    rows = [
        _gate_row("Candidate 1 (`EPISODIC_PIVOT`)", verdicts[Strategy.EPISODIC_PIVOT], ok),
        _gate_row(
            "Candidate 2 (together)",
            verdict([t for s in CANDIDATE_2 for t in by[s]], run.block_of),
            ok,
        ),
        *(_gate_row(f"`{s.value}`", verdicts[s], ok) for s in ORDER),
        _gate_row("Everything", verdict(run.reference, run.block_of), ok),
    ]
    if passing:
        rows.append(
            _gate_row(
                "The passing strategies pooled",
                verdict([t for s in passing for t in by[s]], run.block_of),
                ok,
            )
        )
    return [
        "## The gate (section 8.1)",
        "",
        "Closed trades only. **A strategy passes** if the primary interval (whole entry "
        "sessions, 2,000 draws, seed 38, 90%) is above zero, it is positive in at least "
        f"{MIN_POSITIVE_YEARS} of the {len(TUNING_YEARS)} years and >= 0 without its best year, "
        f"it has at least {MIN_TRADES} closed trades, and the decision-price check is within "
        "its threshold. The secondary interval (21-session blocks) decides nothing.",
        "",
        "| Pool | Trades | Expectancy (R) | 90% interval | 21-session blocks | Positive years | "
        "Without the best year | >= 300 | Result | Win rate | Avg gain (R) | Avg loss (R) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
        *rows,
        "",
        "Passing on tuning: "
        + (", ".join(f"`{s.value}`" for s in passing) if passing else "**none**")
        + ". Open at the end of the data (excluded): "
        + ", ".join(f"`{s.value}` {sum(t.status is Status.OPEN for t in by[s])}" for s in ORDER)
        + ".",
        "",
    ]


def _per_year(by: dict[Strategy, list[PoolTrade]]) -> list[str]:
    lines = [
        "## Per year",
        "",
        "Expectancy (R) and closed trades by entry year.",
        "",
        "| Strategy | " + " | ".join(str(y) for y in TUNING_YEARS) + " |",
        "|---|" + "---|" * len(TUNING_YEARS),
    ]
    for s in ORDER:
        cells = []
        for y in TUNING_YEARS:
            e = expectancy(t for t in by[s] if t.entry_day.year == y)
            cells.append(f"{_r(e.mean)} ({e.trades})")
        lines.append(f"| `{s.value}` | " + " | ".join(cells) + " |")
    return [*lines, ""]


def _per_regime(by: dict[Strategy, list[PoolTrade]]) -> list[str]:
    lines = [
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
    return [*lines, ""]


def _comparisons(
    compared: Sequence[tuple[Variant, list[Difference], list[PoolTrade]]],
) -> list[str]:
    lines = [
        "## Comparisons (section 7)",
        "",
        "Each variant changes one dimension against its candidate and re-plays the pool. The "
        "difference is the variant's expectancy minus the candidate's; its interval resamples "
        "whole entry sessions of both. Adopted only if better by more than noise on both "
        "periods; the holdout opens only if a candidate passes.",
        "",
        "| # | Variant | Strategies | Candidate (R) | Variant (R) | Difference | 90% interval | "
        "Beyond noise |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for v, diffs, _ in compared:
        for d in diffs:
            who = "together" if d.strategy is None else f"`{d.strategy.value}`"
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
    rejected: list[str] = []
    for v, _, played in compared:
        if v.name != "structural_stop":
            continue
        for s in CANDIDATE_2:
            n = sum(
                1
                for t in played
                if t.strategy is s
                and t.result is not None
                and any(f.rule is GuardrailRule.STOP_WIDTH for f in t.result.findings)
            )
            rejected.append(f"`{s.value}` {n:,}")
    if rejected:
        lines += ["", "Stop-width rejections with the structural stop: " + ", ".join(rejected)]
    return [*lines, ""]


def _scored_diagnostics(run: CloseRun, by: dict[Strategy, list[PoolTrade]]) -> list[str]:
    no_cost = _by_strategy(run.no_cost)
    chased = {
        (t.listing.found.security, t.strategy, t.entry_day)
        for t in run.reference
        if t.status is Status.NOT_CONFIRMED
        and t.result is not None
        and t.result.gate is not None
        and set(t.result.gate.failures) == {"price is above the max chase"}
    }
    skipped_by_chase = [
        t for t in run.no_chase if (t.listing.found.security, t.strategy, t.entry_day) in chased
    ]
    chase = _by_strategy(skipped_by_chase)
    lines = [
        "## Diagnostics (section 10; reported, never tuned)",
        "",
        "| Strategy | At zero slippage (R) | >= $100M a day (R) | Below $100M (R) | Skipped by "
        "the max chase, entered anyway (R) | Stopped on the entry session | Within 5 sessions |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in ORDER:
        liquid = [t for t in by[s] if (t.listing.found.security, t.entry_day) in run.liquid]
        thin = [t for t in by[s] if (t.listing.found.security, t.entry_day) not in run.liquid]
        closed = [t for t in by[s] if t.r is not None and t.result is not None]
        early1 = sum(1 for t in closed if _stopped_by(t, 1))
        early5 = sum(1 for t in closed if _stopped_by(t, 5))
        e0, el, et, ec = (expectancy(x) for x in (no_cost[s], liquid, thin, chase[s]))
        lines.append(
            f"| `{s.value}` | {_r(e0.mean)} ({e0.trades}) | {_r(el.mean)} ({el.trades}) | "
            f"{_r(et.mean)} ({et.trades}) | {_r(ec.mean)} ({ec.trades}) | "
            f"{_share(early1, len(closed))} | {_share(early5, len(closed))} |"
        )
    total0 = expectancy(run.no_cost)
    lines += ["", f"Everything at zero slippage: {_r(total0.mean)} over {total0.trades:,} trades."]
    return [*lines, ""]


def _stopped_by(t: PoolTrade, sessions: int) -> bool:
    r = t.result
    if r is None or r.exit is not Rule.HARD_STOP or (t.r or 0) >= 0:
        return False
    return any(f.rule is Rule.HARD_STOP and f.session <= sessions for f in r.fills)


# --- Fixed text -------------------------------------------------------------------------------


def _reference() -> list[str]:
    return [
        "## The 09:36 reference (3.8, not re-run)",
        "",
        "From `docs/backtests/baseline.md`, the EP after the look-ahead fix (#143).",
        "",
        "| Strategy | Trades | Expectancy (R) | 90% interval |",
        "|---|---|---|---|",
        *(f"| {a} | {b} | {c} | {d} |" for a, b, c, d in REFERENCE_0936),
        "",
    ]


def _biases() -> list[str]:
    return [
        "## Known biases",
        "",
        "- **Survivorship:** delisted securities the vendor doesn't cover are missing; "
        "expectancy is an upper bound.",
        "- **Industry groups:** today's SIC is applied to the past.",
        "- **EP proxy:** no earnings-beat or news condition; live EPs require one, so they are "
        "fewer.",
        "- **The decision price is the close** and the volume the full day (measured above).",
        "- **The live small-cap gap:** at 15:40 the IEX spread cap (#120) leaves many small caps "
        "without a decision price; entries fall back to the delayed SIP last trade (#144). The "
        "$100M tier stands in for the names with a usable IEX price.",
        "- **Slippage tiers** are provisional (#141).",
        "- **Single-session blocks** understate the interval's width (the 21-session column).",
        "- **A second look at 2017-2022:** the 3.8 run and its diagnostics were read before "
        "these candidates were chosen; the per-year rule and the holdout are the guards.",
        f"- **Candidates:** 1 = {', '.join(f'`{s.value}`' for s in CANDIDATE_1)}; 2 = "
        + ", ".join(f"`{s.value}`" for s in CANDIDATE_2)
        + ".",
    ]

"""The momentum book's reports as Markdown (task 3.13): the scored run, the blind check and the
holdout. Generated; only the Reading at the end of a scored report is written by hand."""

from __future__ import annotations

import statistics
from collections.abc import Sequence

from auratrade.guardrails.blocks import EntryBlock
from auratrade.sim.book import ExitReason
from auratrade.strategies.momentum_book import SkipReason
from backtest import momentum_report as mr
from backtest.momentum import (
    PREREGISTRATION_COMMIT,
    Blind,
    Holdout,
    Result,
    Scored,
    Window,
)

PREREGISTRATION = "`docs/backtests/momentum-preregistration.md`"


def _pct(x: float | None, digits: int = 1, sign: bool = False) -> str:
    if x is None:
        return "-"
    return f"{x * 100:+.{digits}f}%" if sign else f"{x * 100:.{digits}f}%"


def _num(x: float | None, digits: int = 2) -> str:
    return "-" if x is None else f"{x:.{digits}f}"


def _yes(ok: bool) -> str:
    return "pass" if ok else "**fail**"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    return [
        "| " + " | ".join(header) + " |",
        "|" + "---|" * len(header),
        *("| " + " | ".join(r) + " |" for r in rows),
    ]


def _period(window: Window, s: Scored | None = None) -> str:
    text = f"cash at the close of {window.start}, marked to {window.end}"
    if s is not None:
        logs = [log for log in s.primary.run.rebalances if not log.retry]
        if logs:
            text += f"; rebalances {logs[0].execution} to {logs[-1].execution} ({len(logs)})"
    return text


# --- the scored run ----------------------------------------------------------------------


def _gate_rows(g: mr.Gate) -> list[list[str]]:
    cfg = g.cfg
    return [
        ["G1", "Excess CAGR over SPY > 0", _pct(g.excess_spy, sign=True), _yes(g.g1)],
        ["G2", "Excess CAGR over MTUM ≥ 0", _pct(g.excess_mtum, sign=True), _yes(g.g2)],
        [
            "G3",
            f"Excess over SPY positive in ≥ {cfg.min_positive_years} of the years, and > 0 "
            "without the best year",
            f"{g.positive_years} years; {_pct(g.without_best, sign=True)} without the best",
            _yes(g.g3),
        ],
        [
            "G4",
            f"CAGR ÷ max drawdown ≥ SPY's, and max drawdown ≤ {_pct(cfg.max_drawdown, 0)}",
            f"{_num(g.calmar)} vs SPY {_num(g.spy_calmar)}; {_pct(g.max_drawdown)}",
            _yes(g.g4),
        ],
    ]


def _performance_row(
    name: str, curve: mr.Curve, spy: mr.Performance, mtum: mr.Performance
) -> list[str]:
    p = mr.performance(curve)
    return [
        name,
        _pct(p.cagr, sign=True),
        _pct(p.volatility),
        _pct(p.max_drawdown),
        _num(p.calmar),
        _pct(p.cagr - spy.cagr, sign=True),
        _pct(p.cagr - mtum.cagr, sign=True),
    ]


def _years(s: Scored, result: Result) -> list[str]:
    rows = [
        [
            str(y.year),
            _pct(y.book, sign=True),
            _pct(y.spy, sign=True),
            _pct(y.mtum, sign=True),
            _pct(y.excess, sign=True),
        ]
        for y in mr.by_year(result.curve, s.spy, s.mtum)
    ]
    return _table(["Year", "Book", "SPY", "MTUM", "Excess over SPY"], rows)


def _activity(s: Scored, result: Result) -> list[str]:
    a = mr.activity(result.run, s.position)
    m = mr.moved(result.run, s.next_session)
    exits = {r: sum(t.reason is r for t in result.run.trades) for r in ExitReason}
    open_at_end = sum(t.exited is None for t in result.run.trades)
    rows = [
        ["Buys a year (turnover)", _num(a.buys_per_year, 1)],
        ["Slippage paid a year, of the starting equity", _pct(a.slippage_per_year, 2)],
        ["Mean holding sessions", _num(a.mean_holding_sessions, 0)],
        ["Mean cash share", _pct(a.mean_cash_share)],
        ["Stop-outs a year", _num(a.stop_outs_per_year, 1)],
        ["Largest single weight", _pct(a.largest_weight)],
        ["Buys moved by a half day / by a retry", f"{m.half_day} / {m.retry}"],
        [
            "Exits: stop / rule / ended with a successor / ended / earnings / open at the end",
            " / ".join(str(exits[r]) for r in ExitReason) + f" / {open_at_end}",
        ],
    ]
    return _table(["", "Primary"], rows)


def _comparisons(s: Scored, spy: mr.Performance) -> list[str]:
    rows = []
    for result, j in s.compared:
        p = mr.performance(result.curve)
        interval = (
            "-"
            if j.interval is None
            else (f"{_pct(j.interval.low, sign=True)} to {_pct(j.interval.high, sign=True)}")
        )
        rows.append(
            [
                j.name,
                result.config.change,
                _pct(p.cagr, sign=True),
                _pct(p.max_drawdown),
                _pct(p.cagr - spy.cagr, sign=True),
                _pct(j.mean, sign=True),
                interval,
                j.kind.value,
                "**adopted**" if j.name in s.adopted_names else ("passed" if j.passed else "no"),
            ]
        )
    return _table(
        [
            "",
            "Change",
            "CAGR",
            "Max drawdown",
            "Excess over SPY",
            "Mean difference a year",
            "90% interval",
            "Rule",
            "Adopted",
        ],
        rows,
    )


def _diagnostics(s: Scored, spy: mr.Performance) -> list[str]:
    base = mr.performance(s.primary.curve)
    rows = [["offset 1 (the primary)", "rebalance on the month's first session"]]
    rows[0] += [_pct(base.cagr, sign=True), _pct(base.max_drawdown)]
    rows[0] += [_pct(base.cagr - spy.cagr, sign=True), "-"]
    for r in s.diagnostics:
        p = mr.performance(r.curve)
        rows.append(
            [
                r.config.name,
                r.config.change,
                _pct(p.cagr, sign=True),
                _pct(p.max_drawdown),
                _pct(p.cagr - spy.cagr, sign=True),
                _pct(p.cagr - base.cagr, sign=True),
            ]
        )
    lines = _table(
        ["", "Change", "CAGR", "Max drawdown", "Excess over SPY", "Against the primary"], rows
    )
    offsets = [base.cagr] + [
        mr.performance(r.curve).cagr for r in s.diagnostics if r.config.offset != 1
    ]
    lines += [
        "",
        f"Timing luck: the CAGR spans {_pct(max(offsets) - min(offsets))} across offsets.",
    ]
    return lines


def _equal_weight(s: Scored, spy: mr.Performance, mtum: mr.Performance) -> list[str]:
    ew = s.equal_weight
    sessions = s.position[ew.dates[-1]] - s.position[ew.dates[0]]
    ratio = ew.values[-1] / ew.values[0]
    rate = ratio ** (mr.TRADING_DAYS / sessions) - 1 if sessions > 0 else 0.0
    return [
        f"The equal-weighted ranked set (each E to the next, price only, no costs; it gates "
        f"nothing): CAGR {_pct(rate, sign=True)}, max drawdown {_pct(mr.max_drawdown(ew.values))} "
        f"on period ends; {_pct(rate - spy.cagr, sign=True)} over SPY, "
        f"{_pct(rate - mtum.cagr, sign=True)} over MTUM."
    ]


def render_scored(s: Scored, stamp: str) -> str:
    bt = s.risk.book.backtest
    spy, mtum = mr.performance(s.spy), mr.performance(s.mtum)
    judged = s.adopted or s.primary
    judged_name = (
        f"the adopted configuration ({' + '.join(s.adopted_names)})"
        if s.adopted
        else "the primary (nothing adopted)"
    )
    gate = s.gate_of(judged)
    frozen = s.frozen
    count = 8 + (s.adopted is not None)
    excess = mr.differences(s.primary.curve, s.spy)
    interval = mr.annual_interval(excess, bt)
    lines = [
        "# The momentum book: the scored run (task 3.15)",
        "",
        f"Generated by `uv run python -m backtest.momentum` on {stamp}. Pre-registration: "
        f"{PREREGISTRATION} at commit `{PREREGISTRATION_COMMIT}`; `risk.yaml` hash "
        f"`{s.risk.config_hash[:12]}`. Tuning period: {_period(s.window, s)}. **Committed as "
        "produced, before it is read for meaning** (§2 step 4); the Reading is written after.",
        "",
        f"**Multiple trials:** {count} configurations were run (the primary and C1-C7"
        + (", plus the adopted configuration" if s.adopted else "")
        + "). The best of several runs flatters itself; only the pre-registered rules choose.",
        "",
        "## The gate",
        "",
        f"Judged on **{judged_name}**:",
        "",
        *_table(["", "Condition", "Value", "Result"], _gate_rows(gate)),
        "",
    ]
    if s.adopted is not None:
        lines += [
            "The primary, for reference:",
            "",
            *_table(["", "Condition", "Value", "Result"], _gate_rows(s.gate_of(s.primary))),
            "",
        ]
    if frozen is None:
        lines.append(
            "**Tuning gate: FAIL.** The holdout stays closed, no capital, and no further "
            "variants on 2017-2022 (§9). It comes back to the user."
        )
    else:
        note = (
            " (the adopted configuration failed, so the primary is frozen instead)"
            if (s.adopted is not None and frozen is s.primary)
            else ""
        )
        lines.append(
            f"**Tuning gate: PASS** for {frozen.config.name}{note}. Next: freeze it in "
            "`risk.yaml` and open the holdout once (3.16)."
        )
    lines += [
        "",
        "## Performance",
        "",
        *_table(
            [
                "",
                "CAGR",
                "Volatility",
                "Max drawdown",
                "CAGR ÷ max DD",
                "Excess over SPY",
                "Excess over MTUM",
            ],
            [
                _performance_row("Book (primary)", s.primary.curve, spy, mtum),
                *(
                    [_performance_row("Book (adopted)", s.adopted.curve, spy, mtum)]
                    if s.adopted
                    else []
                ),
                _performance_row("SPY (total return)", s.spy, spy, mtum),
                _performance_row("MTUM (total return)", s.mtum, spy, mtum),
            ],
        ),
        "",
        *_equal_weight(s, spy, mtum),
        "",
        "The primary's excess over SPY, mean daily x 252: "
        f"{_pct(statistics.fmean(excess.values()) * mr.TRADING_DAYS, sign=True)}; 90% "
        f"block-bootstrap interval ({bt.bootstrap_block_sessions}-session blocks, "
        f"{bt.bootstrap_samples} draws): "
        + (
            "-"
            if interval is None
            else f"{_pct(interval.low, sign=True)} to {_pct(interval.high, sign=True)}"
        )
        + f". Minimum detectable annual excess: {_pct(mr.min_detectable(excess))}. Reported; it "
        "gates nothing.",
        "",
        "## Per year (the primary)",
        "",
        *_years(s, s.primary),
        "",
    ]
    if s.adopted is not None:
        lines += ["## Per year (the adopted configuration)", "", *_years(s, s.adopted), ""]
    lines += [
        "## Activity",
        "",
        *_activity(s, s.primary),
        "",
        "## The comparisons (C1-C7, §6)",
        "",
        "Each changes one thing from the primary. Mean difference and interval: the daily return "
        "of the variant minus the primary's, x 252. CHANGE and REMOVE_SAFETY are adopted when the "
        "interval lies wholly above zero and the drawdown is no deeper; ADD_SAFETY (C5) when its "
        "drawdown is shallower and the interval isn't wholly below zero; C3 and C4 can't both be.",
        "",
        *_comparisons(s, spy),
        "",
        "Adopted: "
        + (", ".join(s.adopted_names) if s.adopted_names else "none; the primary stands."),
        "",
        "## Diagnostics (reported, never adopted)",
        "",
        *_diagnostics(s, spy),
        "",
        f"**The signal check:** the monthly rank correlation between the signal on R and the "
        f"return from E to the next E, over the ranked set: mean {_num(s.signal.mean, 3)}, "
        f"t = {_num(s.signal.t)}, {s.signal.months} months.",
        "",
        "**By regime** (the regime computed the evening before; mean daily x 252):",
        "",
        *_table(
            ["Regime", "Sessions", "Book", "SPY", "Excess"],
            [
                [
                    "unknown" if r.regime is None else r.regime.value,
                    str(r.sessions),
                    _pct(r.book, sign=True),
                    _pct(r.spy, sign=True),
                    _pct(r.excess, sign=True),
                ]
                for r in mr.by_regime(s.primary.curve, s.spy, s.regimes)
            ],
        ),
        "",
        *_biases(),
        "",
        "## Reading",
        "",
        "_To be written after the report is committed (pre-registration §2 step 5)._",
    ]
    return "\n".join(lines) + "\n"


def _biases() -> list[str]:
    return [
        "## Known biases and the simulator's readings",
        "",
        "- The book is **price only** against **total-return** benchmarks: it is understated by "
        "about its dividend yield (§4).",
        "- **Industry groups** are today's SIC codes applied to the past (§3.7).",
        "- **C5's report dates** are the EDGAR history, treated as known in advance.",
        "- A security that ends exits against **the next piece of the same ticker** when that is "
        "loaded (§3.8); a piece starting after the period isn't, and the exit takes the last "
        "close.",
        "- The gaps the pre-registration leaves are read as in decisions #150 and #151 "
        "(README §16).",
        "- **Changed after the blind check, with the user's approval (2026-10-26):** the 30% "
        "drawdown halt re-enables at the next E with the peak reset to the book's value there. "
        "Built first without a re-enable, it held the book in cash from late 2018 and tripped "
        "the §7 mean-holdings stop condition (#150).",
    ]


# --- the blind check ---------------------------------------------------------------------


BLOCK_LABELS = {
    EntryBlock.CORRECTION: "CORRECTION",
    EntryBlock.CRISIS_OVERRIDE: "crisis override",
    EntryBlock.INDEX_TREND: "SPY trend",
    EntryBlock.DAILY_LOSS_HALT: "daily halt",
    EntryBlock.WEEKLY_LOSS_HALT: "weekly halt",
    EntryBlock.DRAWDOWN_HALT: "30% halt",
    EntryBlock.EARLY_CLOSE: "half day",
}


def _blind_row(label: str, c: mr.BlindCounts) -> list[str]:
    ranked = f"{min(c.ranked)} / {statistics.median(c.ranked):.0f}" if c.ranked else "-"
    held = f"{statistics.fmean(c.holdings):.2f} / {min(c.holdings)}" if c.holdings else "-"
    per_e = statistics.fmean(c.candidates) if c.candidates else None
    return [
        label,
        str(c.executions),
        ranked,
        _num(per_e, 1),
        str(c.buys),
        held,
        str(c.retries),
        str(c.exits[ExitReason.STOP]),
        f"{c.exits[ExitReason.ENDED_SUCCESSOR]} / {c.exits[ExitReason.ENDED]}",
        _num(statistics.fmean(c.holding_sessions) if c.holding_sessions else None, 0),
    ]


def _skip_row(label: str, c: mr.BlindCounts) -> list[str]:
    reasons = (
        SkipReason.PRICE_FIT,
        SkipReason.NO_CASH,
        SkipReason.CLUSTER,
        SkipReason.NO_PRICE,
        SkipReason.EARNINGS,
    )
    return [
        label,
        *(str(c.skips[r]) for r in reasons),
        *(f"{c.blocked[b]} ({c.blocked_picks[b]})" for b in BLOCK_LABELS),
        str(c.unfilled),
    ]


def render_blind(b: Blind, stamp: str) -> str:
    years = sorted(k for k in b.counts if k is not None)
    overall = b.counts[None]
    stops = b.stops
    lines = [
        "# The momentum book: the blind mechanics check (task 3.14)",
        "",
        f"Generated by `uv run python -m backtest.momentum --blind` on {stamp}. Pre-registration: "
        f"{PREREGISTRATION} at commit `{PREREGISTRATION_COMMIT}`; `risk.yaml` hash "
        f"`{b.risk.config_hash[:12]}`. Tuning period: {_period(b.window)}. Counts and "
        "invariants only (§2 step 3, §7); nothing about the account's value or the gate.",
        "",
        "Changed after the first blind run, with the user's approval (2026-10-26): the 30% halt "
        "counts as re-enabled at the next E, its peak restarting there (#150). The first run "
        "held the book in cash from late 2018 and tripped the mean-holdings stop condition.",
        "",
        "## Invariants",
        "",
        "Held throughout in every configuration run (" + ", ".join(b.checked) + "): at most "
        f"{b.risk.book.holdings} holdings, cash never below zero, every stop below its fill at "
        "entry, no stop lowered, no buy on a blocked session, no fill above its limit, no full "
        "cluster at entry (the simulator raises on any of them).",
        "",
        "## Stop conditions (§7)",
        "",
        *(
            [f"- **Fired:** {s}" for s in stops] + ["", "Work stops here and goes to the user."]
            if stops
            else ["None fired: the scored run (3.15) may go ahead, with the user."]
        ),
        "",
        f"Price skips {_pct(overall.price_skip_share)} of the names the rule book reached "
        f"(limit {_pct(b.risk.book.backtest.blind_max_price_skips, 0)}); cluster skips "
        f"{_pct(overall.cluster_skip_share)} of the candidates (limit "
        f"{_pct(b.risk.book.backtest.blind_max_cluster_skips, 0)}).",
        "",
        "## Counts (the primary)",
        "",
        *_table(
            [
                "Year",
                "E sessions",
                "Ranked (min / median)",
                "Candidates per E",
                "Buys",
                "Holdings after buys (mean / min)",
                "Retries",
                "Stop-outs",
                "Ended (successor / none)",
                "Mean holding sessions",
            ],
            [_blind_row(str(y), b.counts[y]) for y in years] + [_blind_row("All", overall)],
        ),
        "",
        "## Skips and blocks",
        "",
        "Skips by reason; blocked sessions by block, with the rule book's picks they stopped in "
        "brackets; picks whose fill would have been above the limit.",
        "",
        *_table(
            [
                "Year",
                "Price fit",
                "No cash",
                "Cluster",
                "No bar",
                "Earnings",
                *BLOCK_LABELS.values(),
                "Unfilled",
            ],
            [_skip_row(str(y), b.counts[y]) for y in years] + [_skip_row("All", overall)],
        ),
    ]
    return "\n".join(lines) + "\n"


# --- the holdout -------------------------------------------------------------------------


def render_holdout(h: Holdout, stamp: str) -> str:
    spy, mtum = mr.performance(h.spy), mr.performance(h.mtum)
    book = mr.performance(h.result.curve)
    rows = [
        ["H1", "Excess CAGR over SPY > 0", _pct(book.cagr - spy.cagr, sign=True), _yes(h.h1)],
        ["H2", "Excess CAGR over MTUM ≥ 0", _pct(book.cagr - mtum.cagr, sign=True), _yes(h.h2)],
    ]
    years = [
        [
            str(y.year),
            _pct(y.book, sign=True),
            _pct(y.spy, sign=True),
            _pct(y.mtum, sign=True),
            _pct(y.excess, sign=True),
        ]
        for y in mr.by_year(h.result.curve, h.spy, h.mtum)
    ]
    lines = [
        "# The momentum book: the holdout, opened once (task 3.16)",
        "",
        f"Generated by `uv run python -m backtest.momentum --holdout` on {stamp}, for the frozen "
        f"configuration (`risk.yaml` hash `{h.risk.config_hash[:12]}`). Pre-registration: "
        f"{PREREGISTRATION} at commit `{PREREGISTRATION_COMMIT}`. Holdout: {_period(h.window)}. "
        "**The holdout is now spent:** a later change makes any rerun a new trial.",
        "",
        *_table(["", "Condition", "Value", "Result"], rows),
        "",
        f"**Holdout: {'PASS' if h.h1 and h.h2 else 'FAIL'}.** 2023 onward was kind to momentum, "
        "so a pass is weak evidence and a fail is strong (§5).",
        "",
        *_table(
            [
                "",
                "CAGR",
                "Volatility",
                "Max drawdown",
                "CAGR ÷ max DD",
                "Excess over SPY",
                "Excess over MTUM",
            ],
            [
                _performance_row("Book (frozen)", h.result.curve, spy, mtum),
                _performance_row("SPY (total return)", h.spy, spy, mtum),
                _performance_row("MTUM (total return)", h.mtum, spy, mtum),
            ],
        ),
        "",
        *_table(["Year", "Book", "SPY", "MTUM", "Excess over SPY"], years),
        "",
        *_biases(),
    ]
    return "\n".join(lines) + "\n"

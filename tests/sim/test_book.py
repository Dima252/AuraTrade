"""Known-answer books for `sim/book.py` (task 3.12).

A weekday calendar from 2021-01-04: R1 = 01-29, E1 = 02-01, R2 = 02-26, E2 = 03-01. Six names on
gentle trends rank A > B > C > D > E > F on R1; each has its own small zigzag so no two are
correlated. The book is shrunk to 3 holdings, 4 candidates, a 5-session 12-1 (1 skipped) and a
2-session volume average; every dollar volume sits in the cheapest slippage tier (0.05%).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import pytest

from auratrade.config import Regime, RiskConfig, load_risk_config
from auratrade.guardrails.blocks import EntryBlock
from auratrade.guardrails.book import BuyDecision
from auratrade.guardrails.model import Outcome
from auratrade.sim import book as book_module
from auratrade.sim.book import (
    BookInvariantError,
    BookMarket,
    BookRun,
    BookSeries,
    ExitReason,
    simulate_book,
)
from auratrade.strategies.momentum_book import (
    Ranked,
    RuleBookChoice,
    Signal,
    Skipped,
    SkipReason,
)


def _risk() -> RiskConfig:
    base = load_risk_config()
    book = base.book.model_copy(
        update={
            "holdings": 3,
            "candidates": 4,
            "review_candidates": 4,
            "universe_size": 6,
            "formation_sessions": 5,
            "skip_sessions": 1,
            "hold_percentile": 50.0,
        }
    )
    return base.model_copy(
        update={
            "book": book,
            "universe": base.universe.model_copy(update={"dollar_volume_sessions": 2}),
            "sim": base.sim.model_copy(update={"volume_sessions": 2}),
            "guardrails": base.guardrails.model_copy(
                update={"cluster_correlation_sessions": 4, "cluster_correlation": 0.99}
            ),
        }
    )


RISK = _risk()


@dataclass(frozen=True, slots=True)
class Day:
    day: date
    is_early_close: bool = False


def _weekdays(first: date, last: date) -> list[date]:
    out, d = [], first
    while d <= last:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = _weekdays(date(2021, 1, 4), date(2021, 3, 31))
T = {d: i for i, d in enumerate(DAYS)}
R1, E1 = date(2021, 1, 29), date(2021, 2, 1)
R2, E2 = date(2021, 2, 26), date(2021, 3, 1)
START = R1
# Name -> (price on E1, daily step, zigzag multiplier, zigzag phase).
NAMES = {
    "A": (50.0, 0.004, 1, 0),
    "B": (40.0, 0.003, 1, 1),
    "C": (25.0, 0.002, 1, 2),
    "D": (20.0, 0.001, 2, 0),
    "E": (30.0, 0.0, 2, 1),
    "F": (35.0, -0.001, 2, 2),
}

OHLC = tuple[float, float, float, float]


@dataclass
class Spec:
    """One name's bars: the trend path, with closes or whole bars overridden and sessions cut."""

    key: str
    symbol: str
    closes: dict[date, float] = field(default_factory=dict)
    bars: dict[date, OHLC] = field(default_factory=dict)
    first: date = DAYS[0]
    last: date = DAYS[-1]
    missing: set[date] = field(default_factory=set)
    params: tuple[float, float, int, int] | None = None

    def series(self) -> BookSeries:
        anchor, step, mult, phase = self.params or NAMES[self.symbol]
        days = [d for d in DAYS if self.first <= d <= self.last and d not in self.missing]
        o, h, lo, c = [], [], [], []
        for d in days:
            t = T[d]
            price = anchor * (1 + step) ** (t - T[E1]) * (1 + 0.0005 * ((t * mult + phase) % 3 - 1))
            price = self.closes.get(d, price)
            bar = self.bars.get(d, (price, price, price, price))
            o.append(bar[0])
            h.append(bar[1])
            lo.append(bar[2])
            c.append(bar[3])
        return BookSeries(self.key, self.symbol, days, o, h, lo, c, list(c), [1e8] * len(days))


def specs() -> dict[str, Spec]:
    out = {name: Spec(name, name) for name in NAMES}
    for name in NAMES:
        out[name].closes[E1] = NAMES[name][0]
    # February: C collapses and D doubles over R2's 12-1 window, so C leaves the hold zone and
    # D tops the list. A and B close on R2 where they stand on E2, so E2 trips no halt.
    out["C"].closes |= {date(2021, 2, 19): 30.0, date(2021, 2, 25): 21.0, R2: 22.0, E2: 22.0}
    out["D"].closes |= {date(2021, 2, 19): 10.0, date(2021, 2, 25): 20.0, R2: 20.0, E2: 20.0}
    out["A"].closes |= {R2: 60.0, E2: 60.0, date(2021, 3, 2): 60.0}
    out["B"].closes |= {R2: 44.0, E2: 44.0, date(2021, 3, 2): 44.0}
    out["D"].closes[date(2021, 3, 2)] = 20.5
    return out


def market(
    spec: Mapping[str, Spec],
    *,
    regimes: Mapping[date, Regime] | None = None,
    early: frozenset[date] = frozenset(),
    industries: Mapping[str, str] | None = None,
    successors: Mapping[str, str] | None = None,
) -> BookMarket:
    series = {k: s.series() for k, s in spec.items()}
    members: dict[date, set[str]] = {}
    for key, s in series.items():
        for d in s.dates:
            members.setdefault(d, set()).add(key)
    return BookMarket(
        sessions=[Day(d, d in early) for d in DAYS],
        series=series,
        members=members,
        regimes=regimes or dict.fromkeys(DAYS, Regime.CONFIRMED_UPTREND),
        industries=industries or {},
        successors=successors or {},
    )


def run(m: BookMarket, end: date, risk: RiskConfig = RISK, equity: float = 10_000.0) -> BookRun:
    out = simulate_book(m, risk, start=START, end=end, equity=equity)
    assert_invariants(out, risk)
    return out


def assert_invariants(out: BookRun, risk: RiskConfig) -> None:
    """The pre-registration's §7 invariants, read back from the output."""
    for mark in out.marks:
        assert mark.holdings <= risk.book.holdings
        assert mark.cash >= -1e-9
    for t in out.trades:
        assert t.entry_price <= t.limit
        assert t.stop < t.entry_price
    for log in out.rebalances:
        if log.blocks:
            assert log.bought == 0


def by_key(out: BookRun) -> dict[str, list[object]]:
    return {t.key: [t.shares, t.entry_price, t.stop, t.entered] for t in out.trades}


class TestMonthOfFills:
    def test_e1_buys_the_top_three_at_the_close_plus_slippage(self) -> None:
        out = run(market(specs()), E1)
        # Slot 10,000 / 3; limit = close x 1.005 up to the tick; shares on the limit, floored;
        # C sized on what the limits of A and B left (3,387.10 > the slot).
        assert by_key(out) == {
            "A": [66, 50.03, 40.0, E1],
            "B": [82, 40.02, 32.0, E1],
            "C": [132, 25.02, 20.0, E1],
        }
        assert out.marks[-1].cash == pytest.approx(10_000 - 3301.98 - 3281.64 - 3302.64)
        log = out.rebalances[0]
        assert (log.ranking, log.execution, log.ranked, log.candidates, log.bought) == (
            R1,
            E1,
            6,
            4,
            3,
        )
        assert log.blocks == frozenset()
        assert out.trades[0].slippage == pytest.approx(66 * 0.03)

    def test_e2_sells_out_of_the_hold_zone_raises_stops_and_refills(self) -> None:
        out = run(market(specs()), E2)
        c = next(t for t in out.trades if t.key == "C")
        assert (c.exited, c.exit_price, c.reason) == (E2, 21.98, ExitReason.RULE)
        assert c.proceeds == pytest.approx(132 * 21.98)
        # 113.74 + 2,901.36 left; equity 10,583.10 -> a 3,527.70 slot, capped by the cash.
        d = next(t for t in out.trades if t.key == "D")
        assert (d.shares, d.entry_price, d.limit, d.stop, d.entered) == (150, 20.01, 20.1, 16.0, E2)
        assert out.marks[-1].cash == pytest.approx(13.60)
        assert out.marks[-1].equity == pytest.approx(13.60 + 66 * 60 + 82 * 44 + 150 * 20)
        log = out.rebalances[-1]
        assert (log.rule_sells, log.stop_raises, log.bought, log.holdings) == (1, 2, 1, 3)
        assert [m.day for m in out.marks] == [d for d in DAYS if START <= d <= E2]

    def test_raised_stops_take_effect_after_the_rebalance(self) -> None:
        s = specs()
        # A's raised stop is 48 (60 x 0.8); B's 35.20. A trades at 47.99 the next session.
        s["A"].bars[date(2021, 3, 2)] = (50.0, 50.0, 47.99, 49.0)
        out = run(market(s), date(2021, 3, 2))
        a = next(t for t in out.trades if t.key == "A")
        assert (a.exited, a.exit_price, a.reason) == (date(2021, 3, 2), 47.97, ExitReason.STOP)


class TestStops:
    def test_a_gap_through_the_stop_fills_at_the_open(self) -> None:
        s = specs()
        day = date(2021, 2, 10)
        s["A"].bars[day] = (35.0, 36.0, 34.0, 36.0)  # stop 40: gaps through
        s["B"].bars[day] = (41.0, 41.0, 31.5, 41.0)  # stop 32: trades through it
        out = run(market(s), date(2021, 2, 12))
        a, b = (next(t for t in out.trades if t.key == k) for k in "AB")
        assert (a.exited, a.exit_price, a.reason) == (day, 34.98, ExitReason.STOP)
        assert a.proceeds == pytest.approx(66 * 34.98)
        assert (b.exit_price, b.reason) == (31.98, ExitReason.STOP)
        # The slot stays cash until the next E.
        assert out.marks[-1].holdings == 1

    def test_no_stop_on_the_buy_session(self) -> None:
        s = specs()
        s["A"].bars[E1] = (50.0, 50.0, 30.0, 50.0)
        out = run(market(s), E1)
        assert next(t for t in out.trades if t.key == "A").exited is None


class TestSecuritiesThatEnd:
    def test_a_delisting_exits_at_its_last_close(self) -> None:
        s = specs()
        s["B"].last = date(2021, 2, 10)
        s["B"].closes[date(2021, 2, 10)] = 41.0
        out = run(market(s), date(2021, 2, 12))
        b = next(t for t in out.trades if t.key == "B")
        assert (b.exited, b.exit_price, b.reason) == (date(2021, 2, 11), 40.97, ExitReason.ENDED)
        assert b.proceeds == pytest.approx(82 * 40.97)

    @pytest.mark.parametrize(
        ("successor_open", "exit_price"),
        [(8.0, 7.99), (410.0, 40.97)],  # a collapse; an unadjusted 1:10 reverse split
    )
    def test_a_jump_ending_takes_the_lower_of_last_close_and_successor_open(
        self, successor_open: float, exit_price: float
    ) -> None:
        s = specs()
        s["B"].last = date(2021, 2, 10)
        s["B"].closes[date(2021, 2, 10)] = 41.0
        s["B2"] = Spec(
            "B2",
            "B",
            first=date(2021, 2, 11),
            params=(successor_open, 0.0, 1, 1),
            bars={date(2021, 2, 11): (successor_open,) * 4},
        )
        out = run(market(s, successors={"B": "B2"}), date(2021, 2, 12))
        b = next(t for t in out.trades if t.key == "B")
        assert (b.exited, b.exit_price) == (date(2021, 2, 11), exit_price)
        assert b.reason is ExitReason.ENDED_SUCCESSOR


class TestCalendarAndSkips:
    def test_a_half_day_moves_e_to_the_next_full_session(self) -> None:
        out = run(market(specs(), early=frozenset({E1})), date(2021, 2, 3))
        log = out.rebalances[0]
        assert (log.ranking, log.execution) == (R1, date(2021, 2, 2))
        assert {t.entered for t in out.trades} == {date(2021, 2, 2)}

    def test_a_name_too_dear_for_a_slot_is_skipped_for_the_next(self) -> None:
        s = specs()
        s["A"].closes[E1] = 5000.0
        out = run(market(s), E1)
        assert sorted(by_key(out)) == ["B", "C", "D"]
        assert Skipped("A", SkipReason.PRICE_FIT) in out.rebalances[0].skipped

    def test_the_cluster_cap_passes_over_a_third_linked_name(self) -> None:
        out = run(market(specs(), industries=dict.fromkeys("ABC", "SEMIS")), E1)
        assert sorted(by_key(out)) == ["A", "B", "D"]
        assert Skipped("C", SkipReason.CLUSTER) in out.rebalances[0].skipped

    def test_the_cluster_cap_off_is_comparison_c7(self) -> None:
        m = market(specs(), industries=dict.fromkeys("ABC", "SEMIS"))
        out = simulate_book(m, RISK, start=START, end=E1, equity=10_000.0, clusters=False)
        assert sorted(by_key(out)) == ["A", "B", "C"]


class TestBlocksAndRetry:
    def test_a_loss_halt_on_e_blocks_buys_and_they_retry_next_session(self) -> None:
        s = specs()
        s["A"].closes[E2] = 40.5  # the book falls ~12% on E2
        out = run(market(s), date(2021, 3, 2))
        e2, retry = out.rebalances[-2:]
        assert e2.blocks == {EntryBlock.DAILY_LOSS_HALT, EntryBlock.WEEKLY_LOSS_HALT}
        assert (e2.rule_sells, e2.picks, e2.bought, e2.retried) == (1, 1, 0, True)
        assert (retry.session, retry.retry, retry.blocks, retry.bought) == (
            date(2021, 3, 2),
            True,
            frozenset(),
            1,
        )
        # Fresh sizes at the retry's close: 3,015.10 cash at a 20.61 limit.
        d = next(t for t in out.trades if t.key == "D")
        assert (d.entered, d.shares, d.entry_price, d.limit) == (
            date(2021, 3, 2),
            146,
            20.52,
            20.61,
        )

    def test_the_drawdown_halt_blocks_e_and_its_retry(self) -> None:
        s = specs()
        s["A"].closes[E2] = 40.5
        risk = RISK.model_copy(update={"book": RISK.book.model_copy(update={"drawdown_halt": 0.1})})
        out = run(market(s), date(2021, 3, 2), risk)
        assert EntryBlock.DRAWDOWN_HALT in out.rebalances[-1].blocks
        assert not out.rebalances[-1].retried
        assert "D" not in by_key(out)

    def test_a_half_day_blocks_the_retry(self) -> None:
        s = specs()
        s["A"].closes[E2] = 40.5
        out = run(market(s, early=frozenset({date(2021, 3, 2)})), date(2021, 3, 3))
        retry = out.rebalances[-1]
        assert retry.retry
        assert EntryBlock.EARLY_CLOSE in retry.blocks
        assert "D" not in by_key(out)

    def test_the_overlay_blocks_without_a_retry(self) -> None:
        regimes = dict.fromkeys(DAYS, Regime.CONFIRMED_UPTREND) | {R2: Regime.CORRECTION}
        out = run(market(specs(), regimes=regimes), date(2021, 3, 2))
        e2 = out.rebalances[-1]
        assert (e2.blocks, e2.picks, e2.bought, e2.retried) == (
            frozenset({EntryBlock.CORRECTION}),
            1,
            0,
            False,
        )
        # Sells and stop raises carry on.
        assert (e2.rule_sells, e2.stop_raises) == (1, 2)

    def test_names_without_a_bar_on_e_are_retried_for_the_empty_slots(self) -> None:
        s = specs()
        for name in "ABCD":
            s[name].missing.add(E1)
        out = run(market(s), date(2021, 2, 2))
        e1, retry = out.rebalances
        assert Skipped("A", SkipReason.NO_PRICE) in e1.skipped
        assert (e1.bought, e1.retried) == (2, True)
        assert [t.key for t in out.trades] == ["E", "F", "A"]
        assert out.trades[-1].entered == retry.session == date(2021, 2, 2)


class TestDataEdges:
    def test_a_rule_sell_without_a_bar_on_e_sells_at_the_next_close(self) -> None:
        s = specs()
        s["C"].missing.add(E2)
        s["A"].missing.add(E2)  # kept, but no bar: no stop raise
        out = run(market(s), date(2021, 3, 2))
        c = next(t for t in out.trades if t.key == "C")
        e2 = out.rebalances[-1]
        assert (e2.rule_sells, e2.stop_raises, e2.holdings) == (1, 1, 3)
        assert (c.exited, c.reason) == (date(2021, 3, 2), ExitReason.RULE)

    def test_a_missing_bar_mid_series_keeps_the_holding_at_its_last_close(self) -> None:
        s = specs()
        s["A"].missing.add(date(2021, 2, 10))
        out = run(market(s), date(2021, 2, 10))
        assert next(t for t in out.trades if t.key == "A").exited is None
        before = next(m for m in out.marks if m.day == date(2021, 2, 9))
        a_moves = out.marks[-1].equity - before.equity
        series = market(s).series

        def move(key: str) -> float:
            close = dict(zip(series[key].dates, series[key].close, strict=True))
            return close[date(2021, 2, 10)] - close[date(2021, 2, 9)]

        others = 82 * move("B") + 132 * move("C")
        assert a_moves == pytest.approx(others)

    def test_two_retries_when_configured(self) -> None:
        s = specs()
        s["A"].closes[E2] = 40.5
        s["B"].closes[date(2021, 3, 2)] = 35.5  # another loss halt on the first retry
        for name, price in (("A", 60.0), ("B", 44.0), ("D", 21.0)):
            s[name].closes[date(2021, 3, 3)] = price
        book = RISK.book.model_copy(update={"buy_retry_sessions": 2})
        out = run(market(s), date(2021, 3, 3), RISK.model_copy(update={"book": book}))
        assert [(x.session, x.retry, x.retried) for x in out.rebalances[-3:]] == [
            (E2, False, True),
            (date(2021, 3, 2), True, True),
            (date(2021, 3, 3), True, False),
        ]
        assert next(t for t in out.trades if t.key == "D").entered == date(2021, 3, 3)

    def test_a_fill_above_the_limit_is_not_bought(self) -> None:
        tiers = (
            RISK.sim.slippage[0].model_copy(update={"min_dollar_volume": 0, "fraction": 0.01}),
        )
        risk = RISK.model_copy(update={"sim": RISK.sim.model_copy(update={"slippage": tiers})})
        out = run(market(specs()), E1, risk)
        assert (out.rebalances[0].picks, out.rebalances[0].unfilled, out.trades) == (3, 3, ())

    def test_cluster_closes_carry_over_a_missing_session_and_unknown_members_are_ignored(
        self,
    ) -> None:
        s = specs()
        s["A"].missing.add(date(2021, 1, 27))
        m = market(s)
        members = {d: set(keys) for d, keys in m.members.items()}
        members[R1] |= {"NOT_IN_HISTORY"}
        s["G"] = Spec("G", "G", first=date(2021, 2, 1), params=(10.0, 0.0, 1, 0))
        m = BookMarket(
            m.sessions,
            m.series | {"G": s["G"].series()},
            members | {R1: members[R1] | {"G"}},
            m.regimes,
        )
        book = book_module._Book(m, RISK, equity=1e4, signal=Signal.TWELVE_ONE, clusters=True)
        closes = list(book.member("A", R1).closes)
        assert closes[1] == closes[2]  # 01-27 carries 01-26's close
        assert len(book.ranking(R1)) == 6
        lazy = book_module._Members(book, {"A": "A", "B": "B"}, R1)
        assert (len(lazy), list(lazy)) == (2, ["A", "B"])

    def test_the_dollar_volume_needs_a_full_window(self) -> None:
        s = specs()["A"].series()
        assert book_module._dollar_volume(s, 0, 2) is None
        assert book_module._dollar_volume(s, 1, 2) == pytest.approx(
            (s.close[0] + s.close[1]) / 2 * 1e8
        )


class TestInvariants:
    def test_a_buy_on_a_blocked_session_is_a_bug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def approve_all(buys: list[book_module.BookBuy], **_: object) -> list[BuyDecision]:
            return [BuyDecision(b.symbol, Outcome.APPROVE, (), 1, 100.0, 1.0) for b in buys]

        monkeypatch.setattr(book_module, "evaluate_buys", approve_all)
        regimes = dict.fromkeys(DAYS, Regime.CORRECTION)
        with pytest.raises(BookInvariantError, match="despite"):
            simulate_book(market(specs(), regimes=regimes), RISK, start=START, end=E1, equity=1e4)

    def test_negative_cash_is_a_bug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def oversize(buys: list[book_module.BookBuy], **_: object) -> list[BuyDecision]:
            return [BuyDecision(b.symbol, Outcome.APPROVE, (), 1000, 100.0, 1.0) for b in buys]

        monkeypatch.setattr(book_module, "evaluate_buys", oversize)
        with pytest.raises(BookInvariantError, match="cash"):
            simulate_book(market(specs()), RISK, start=START, end=E1, equity=1e4)

    @pytest.mark.parametrize(
        ("limit", "stop", "message"),
        [(None, 1.0, "without levels"), (100.0, 1000.0, "not below the fill")],
    )
    def test_bad_approved_levels_are_a_bug(
        self, monkeypatch: pytest.MonkeyPatch, limit: float | None, stop: float, message: str
    ) -> None:
        def approve(buys: list[book_module.BookBuy], **_: object) -> list[BuyDecision]:
            return [BuyDecision(b.symbol, Outcome.APPROVE, (), 1, limit, stop) for b in buys]

        monkeypatch.setattr(book_module, "evaluate_buys", approve)
        with pytest.raises(BookInvariantError, match=message):
            simulate_book(market(specs()), RISK, start=START, end=E1, equity=1e4)

    def test_a_buy_into_a_full_cluster_is_a_bug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(book_module, "cluster_check", lambda *_: object())
        with pytest.raises(BookInvariantError, match="full cluster"):
            simulate_book(market(specs()), RISK, start=START, end=E1, equity=1e4)

    def test_too_many_holdings_is_a_bug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = book_module.rule_book_choice

        def every_candidate(listed: list[Ranked], **kwargs: Any) -> RuleBookChoice:
            return real(listed, **(kwargs | {"open_slots": len(listed)}))

        def approve(buys: list[book_module.BookBuy], **_: object) -> list[BuyDecision]:
            return [BuyDecision(b.symbol, Outcome.APPROVE, (), 1, 100.0, 1.0) for b in buys]

        monkeypatch.setattr(book_module, "rule_book_choice", every_candidate)
        monkeypatch.setattr(book_module, "evaluate_buys", approve)
        with pytest.raises(BookInvariantError, match="holdings"):
            simulate_book(market(specs()), RISK, start=START, end=E1, equity=1e4)

    def test_a_lowered_stop_is_a_bug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(book_module, "raised_stop", lambda current, price, cfg: current / 2)
        with pytest.raises(BookInvariantError, match="lowered"):
            simulate_book(market(specs()), RISK, start=START, end=E2, equity=1e4)

    def test_start_and_end_must_be_sessions(self) -> None:
        with pytest.raises(ValueError, match="sessions"):
            simulate_book(market(specs()), RISK, start=date(2021, 1, 30), end=E1, equity=1e4)

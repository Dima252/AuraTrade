from dataclasses import dataclass
from datetime import date
from typing import ClassVar

import pytest

from auratrade.config import BookConfig, GuardrailsConfig, load_risk_config
from auratrade.guardrails.clusters import ClusterMember
from auratrade.strategies.momentum_book import (
    BookSecurity,
    Pick,
    Ranked,
    Rebalance,
    Signal,
    Skipped,
    SkipReason,
    buy_limit,
    candidates,
    high_52w,
    initial_stop,
    keeps,
    raised_stop,
    rank,
    ranked_set,
    rebalances,
    rule_book_choice,
    shares_for,
    slot_value,
    twelve_one,
)

RISK = load_risk_config()
# A 5-session formation with a 1-session skip keeps the hand-built series short.
CFG: BookConfig = RISK.book.model_copy(
    update={"formation_sessions": 5, "skip_sessions": 1, "universe_size": 4}
)
# Correlation over 4 returns; only near-identical moves link two names.
GUARD: GuardrailsConfig = RISK.guardrails.model_copy(
    update={"cluster_correlation_sessions": 4, "cluster_correlation": 0.99}
)
SLIP = 0.005


def sec(
    symbol: str, closes: list[float], volume: float | None = 1e8, *, member: bool = True
) -> BookSecurity:
    return BookSecurity(symbol, symbol, closes, volume, member)


def ranked(symbol: str, signal: float = 0.1, percentile: float = 95.0, n: int = 1) -> Ranked:
    return Ranked(symbol, symbol, signal, percentile, n)


# Each name's own zigzag, so returns differ unless a test makes them the same.
PATTERNS = {
    "A": [10, 11, 10, 11, 10],
    "B": [10, 10, 11, 10, 11],
    "C": [10, 12, 11, 11, 13],
    "D": [10, 9, 10, 12, 11],
    "E": [10, 10, 10, 11, 12],
    "F": [10, 13, 10, 13, 10],
}


def member(symbol: str, industry: str | None = None) -> ClusterMember:
    return ClusterMember(symbol, industry, [float(c) for c in PATTERNS[symbol]])


class TestSignals:
    def test_twelve_one_skips_the_last_bars(self) -> None:
        # close[-2] / close[-6] - 1 = 15 / 10 - 1
        assert twelve_one([10, 11, 12, 13, 15, 99], CFG) == pytest.approx(0.5)

    @pytest.mark.parametrize(
        "closes", [[10, 11, 12, 13, 14], [9, 0, 12, 13, 14, 15, 16], [10, 11, 12, 13, 14, 0, 16]]
    )
    def test_twelve_one_undefined(self, closes: list[float]) -> None:
        assert twelve_one(closes, CFG) is None

    def test_high_52w(self) -> None:
        # the last 5 closes: 20, 16, 18, 15, 16 -> 16 / 20
        assert high_52w([99, 20, 16, 18, 15, 16], CFG) == pytest.approx(0.8)

    @pytest.mark.parametrize("closes", [[1, 2, 3, 4, 5], [99, 20, 16, -1, 15, 16]])
    def test_high_52w_undefined(self, closes: list[float]) -> None:
        assert high_52w(closes, CFG) is None


class TestRankedSet:
    def test_filters_and_cuts_by_dollar_volume(self) -> None:
        long = [10.0] * 6
        securities = [
            sec("OUT", long, 9e9, member=False),
            sec("SHORT", [10.0] * 5, 9e9),
            sec("NOVOL", long, None),
            sec("NAN", long, float("nan")),
            sec("B", long, 5e8),
            sec("A", long, 5e8),
            sec("C", long, 4e8),
            sec("D", long, 3e8),
            sec("E", long, 2e8),
        ]
        assert [s.symbol for s in ranked_set(securities, CFG)] == ["A", "B", "C", "D"]


class TestRank:
    def test_order_percentiles_and_ties(self) -> None:
        out = rank(
            [
                sec("LOW", [10, 10, 10, 10, 10, 9, 9]),
                sec("ZED", [10, 10, 10, 10, 10, 12, 12]),
                sec("ABE", [10, 10, 10, 10, 10, 12, 12]),
                sec("TOP", [10, 10, 10, 10, 10, 15, 15]),
            ],
            CFG,
        )
        assert [(r.symbol, r.rank) for r in out] == [("TOP", 1), ("ABE", 2), ("ZED", 3), ("LOW", 4)]
        assert [r.percentile for r in out] == pytest.approx([100.0, 50.0, 50.0, 0.0])
        assert out[0].signal == pytest.approx(0.5)

    def test_high_52w_ties_break_on_twelve_one(self) -> None:
        out = rank(
            [
                sec("AAA", [10, 10, 11, 12, 13, 14, 15]),  # at its high, 12-1 = 14/10 - 1
                sec("BBB", [10, 10, 14, 14, 14, 14, 15]),  # at its high, 12-1 = 14/10 - 1 too
                sec("CCC", [5, 5, 6, 7, 8, 9, 10]),  # at its high, 12-1 = 9/5 - 1: first
            ],
            CFG,
            Signal.HIGH_52W,
        )
        assert [r.symbol for r in out] == ["CCC", "AAA", "BBB"]
        assert all(r.signal == pytest.approx(1.0) for r in out)

    def test_undefined_signals_are_left_out(self) -> None:
        out = rank(
            [
                sec("BAD", [10, 0, 10, 10, 10, 10, 10]),  # 12-1 undefined
                sec("HOLE", [10, 10, 10, -1, 10, 11, 12]),  # 12-1 fine, the high window isn't
                sec("OK", [10, 10, 10, 10, 10, 11, 12]),
            ],
            CFG,
            Signal.HIGH_52W,
        )
        assert [r.symbol for r in out] == ["OK"]
        assert out[0].percentile == 50.0

    def test_empty(self) -> None:
        assert rank([sec("BAD", [1, 0, 1, 1, 1, 1, 1])], CFG) == []


class TestKeeps:
    def test_hold_zone(self) -> None:
        by_key = {"IN": ranked("IN", percentile=90.0), "OUT": ranked("OUT", percentile=89.9)}
        assert keeps("IN", by_key, CFG)
        assert not keeps("OUT", by_key, CFG)
        assert not keeps("GONE", by_key, CFG)


@dataclass(frozen=True)
class Day:
    day: date
    is_early_close: bool = False


class TestRebalances:
    SESSIONS: ClassVar[list[Day]] = [
        Day(date(2024, 10, 30)),
        Day(date(2024, 10, 31)),
        Day(date(2024, 11, 1), is_early_close=True),  # a half day on the first session
        Day(date(2024, 11, 4)),
        Day(date(2024, 11, 29)),
        Day(date(2024, 12, 2)),
        Day(date(2024, 12, 3)),
        Day(date(2024, 12, 31)),
        Day(date(2025, 1, 2), is_early_close=True),  # no full session after it in range
    ]

    def test_first_session_of_each_month(self) -> None:
        assert rebalances(self.SESSIONS) == [
            Rebalance(date(2024, 10, 31), date(2024, 11, 4)),  # E moves, R doesn't
            Rebalance(date(2024, 11, 29), date(2024, 12, 2)),
        ]

    def test_offset(self) -> None:
        # The 2nd session: October's is in range (R = 10-30), November's is 11-04.
        assert rebalances(self.SESSIONS, offset=2) == [
            Rebalance(date(2024, 10, 30), date(2024, 10, 31)),
            Rebalance(date(2024, 11, 1), date(2024, 11, 4)),
            Rebalance(date(2024, 12, 2), date(2024, 12, 3)),
        ]

    def test_bad_offset(self) -> None:
        with pytest.raises(ValueError, match="offset"):
            rebalances(self.SESSIONS, offset=0)


class TestLevels:
    def test_slot_value(self) -> None:
        assert slot_value(5000, CFG) == pytest.approx(625.0)
        assert slot_value(5000, CFG, 0.5) == pytest.approx(312.5)
        assert slot_value(-10, CFG) == 0.0

    def test_buy_limit_rounds_up(self) -> None:
        assert buy_limit(100.0, SLIP) == 100.5
        assert buy_limit(10.01, SLIP) == 10.07  # 10.06005 -> up to the tick

    def test_shares(self) -> None:
        assert shares_for(625.0, 62.5) == 10
        assert shares_for(625.0, 700.0) == 0
        assert shares_for(0.3 * 3, 0.3) == 3  # float noise doesn't floor 3 to 2
        assert shares_for(float("nan"), 10.0) == 0
        assert shares_for(100.0, 0.0) == 0

    def test_stops(self) -> None:
        assert initial_stop(100.0, CFG) == 80.0
        assert initial_stop(10.07, CFG) == 8.05  # 8.056 -> down to the tick
        assert raised_stop(80.0, 120.0, CFG) == 96.0
        assert raised_stop(96.0, 110.0, CFG) == 96.0  # never lower


class TestCandidates:
    def test_filters_in_rank_order(self) -> None:
        ranking = [ranked(s, n=i) for i, s in enumerate("ABCDEF", start=1)]
        out = candidates(
            ranking,
            held={"A"},
            prices={"B": None, "C": 900.0, "D": 50.0, "E": 50.0, "F": 50.0},
            slot=625.0,
            limit_slippage=SLIP,
            holdings=[member("A", "tech"), member("B", "tech")],
            members={"D": member("D", "tech"), "E": member("E", "energy"), "F": member("F")},
            guard=GUARD,
            count=2,
        )
        assert [r.symbol for r in out.candidates] == ["E", "F"]
        assert out.skipped == (
            Skipped("B", SkipReason.NO_PRICE),
            Skipped("C", SkipReason.PRICE_FIT),
            Skipped("D", SkipReason.CLUSTER),
        )

    def test_missing_member_counts_as_linked_and_count_stops_early(self) -> None:
        ranking = [ranked("D", n=1), ranked("E", n=2), ranked("F", n=3)]
        prices = {"D": 50.0, "E": 50.0, "F": 50.0}
        out = candidates(
            ranking,
            held=set(),
            prices=prices,
            slot=625.0,
            limit_slippage=SLIP,
            holdings=[],
            members={"E": member("E")},
            guard=GUARD,
            count=1,
        )
        assert [r.symbol for r in out.candidates] == ["E"]
        assert out.skipped == (Skipped("D", SkipReason.CLUSTER),)

    def test_clusters_off(self) -> None:
        out = candidates(
            [ranked("D")],
            held=set(),
            prices={"D": 50.0},
            slot=625.0,
            limit_slippage=SLIP,
            holdings=[member("A", "tech"), member("B", "tech")],
            members={},
            guard=GUARD,
            count=16,
            clusters=False,
        )
        assert [r.symbol for r in out.candidates] == ["D"]


class TestRuleBook:
    def test_fills_in_order_rechecking_clusters_and_cash(self) -> None:
        listed = [ranked(s, n=i) for i, s in enumerate("CDEF", start=1)]
        out = rule_book_choice(
            listed,
            open_slots=2,
            cash=700.0,
            slot=625.0,
            prices={"C": 100.0, "D": 50.0, "E": 200.0, "F": 9.0},
            limit_slippage=SLIP,
            holdings=[member("A", "tech")],
            members={
                "C": member("C", "tech"),
                "D": member("D", "tech"),  # A + C already: rejected
                "E": member("E", "energy"),
                "F": member("F", "energy"),
            },
            guard=GUARD,
            cfg=CFG,
        )
        # C: 625 / 100.5 -> 6 shares (603.00), 97 cash left; E at 201 -> 0 shares;
        # F: 97 / 9.05 -> 10 shares.
        assert out.picks == (
            Pick(listed[0], 6, 100.5, 80.0),
            Pick(listed[3], 10, 9.05, 7.2),
        )
        assert out.skipped == (
            Skipped("D", SkipReason.CLUSTER),
            Skipped("E", SkipReason.NO_CASH),
        )

    def test_open_slots_and_clusters_off(self) -> None:
        listed = [ranked("C", n=1), ranked("D", n=2)]
        out = rule_book_choice(
            listed,
            open_slots=1,
            cash=5000.0,
            slot=625.0,
            prices={"C": 100.0, "D": 50.0},
            limit_slippage=SLIP,
            holdings=[],
            members={},
            guard=GUARD,
            cfg=CFG,
            clusters=False,
        )
        assert [p.ranked.symbol for p in out.picks] == ["C"]
        assert out.skipped == ()

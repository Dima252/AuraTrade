from dataclasses import replace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from auratrade.config import DEFAULT_RISK_CONFIG, load_risk_config
from auratrade.indicators.bars import Bar
from auratrade.indicators.bases import (
    BaseCountState,
    BaseRules,
    BaseStatus,
    Contraction,
    PriceBase,
    detect_bases,
)

# The production values except a 5-session minimum, so hand-built paths stay short.
RULES = BaseRules(
    min_sessions=5,
    max_depth=0.50,
    swing_reversal=0.03,
    pivot_min_position=0.5,
    new_stage_advance=0.20,
)


def bars(closes: list[float], lows: dict[int, float] | None = None) -> list[Bar]:
    """Zero-range bars at each close, except the given intraday lows."""
    lows = lows or {}
    return [
        Bar(open=c, high=c, low=lows.get(i, c), close=c, volume=1000) for i, c in enumerate(closes)
    ]


def seg(start: float, end: float, n: int) -> list[float]:
    """``n`` evenly spaced values after ``start`` (excluded) up to ``end`` (included)."""
    return [round(start + (end - start) * k / n, 6) for k in range(1, n + 1)]


def base_path(top: float, low: float, back_to: float, breakout: float) -> list[float]:
    """From ``top`` (excluded): down to ``low`` in 4, back up to ``back_to`` in 4, then the
    breakout close."""
    return [*seg(top, low, 4), *seg(low, back_to, 4), breakout]


def broken_out(scan_bases: tuple[PriceBase, ...]) -> list[PriceBase]:
    return [b for b in scan_bases if b.status is BaseStatus.BROKEN_OUT]


class TestRules:
    @pytest.mark.parametrize(
        "change",
        [
            {"min_sessions": 0},
            {"max_depth": 1.0},
            {"swing_reversal": 0.0},
            {"pivot_min_position": 1.5},
            {"new_stage_advance": 0.0},
        ],
    )
    def test_invalid_values_are_rejected(self, change: dict[str, float]) -> None:
        with pytest.raises(ValueError, match="must be"):
            replace(RULES, **change)

    def test_risk_yaml_builds_the_rules(self) -> None:
        rules = BaseRules(**load_risk_config(DEFAULT_RISK_CONFIG).bases.model_dump())
        assert rules.min_sessions == 25
        assert rules.new_stage_advance == 0.20


def test_empty_history() -> None:
    scan = detect_bases([], RULES)
    assert scan.bases == ()
    assert scan.forming is None
    assert scan.state == BaseCountState()


class TestDetection:
    def test_a_single_base(self) -> None:
        # An advance to 100 (each close above the last ends a 1-session candidate), a 10%
        # pullback to 90, a recovery to 99 and a breakout above the 100 pivot at session 12.
        closes = [80, 85, 90, 95, 100, 97, 94, 91, 90, 93, 96, 99, 101]
        scan = detect_bases(bars(closes), RULES)
        assert scan.bases == (
            PriceBase(
                start=4,
                end=12,
                status=BaseStatus.BROKEN_OUT,
                sessions=8,
                high=100,
                low=90,
                low_session=8,
                pivot=100,
                contractions=(Contraction(4, 100, 8, 90),),
                stage=1,
                count_reliable=False,
                advance=None,
                reset=False,
            ),
        )
        assert scan.bases[0].depth == pytest.approx(0.10)
        assert scan.forming is not None
        assert (scan.forming.start, scan.forming.sessions) == (12, 1)
        assert scan.state == BaseCountState(last_stage=1, last_low=90, last_pivot=100)

    def test_vcp_contractions_and_the_final_contraction_pivot(self) -> None:
        # 20% -> 10% -> 5%: the pivot is the final contraction's high (94), not the base high.
        closes = [100, 90, 80, 88, 96, 91, 86.4, 90, 94, 91.5, 89.3, 92, 95]
        (base,) = detect_bases(bars(closes), RULES).bases
        assert [(c.high, c.low) for c in base.contractions] == [(100, 80), (96, 86.4), (94, 89.3)]
        assert [round(c.depth, 3) for c in base.contractions] == [0.2, 0.1, 0.05]
        assert base.pivot == 94
        assert base.end == 12

    def test_a_lower_half_rally_is_not_a_breakout(self) -> None:
        # The last swing high (80) is below the middle of the 70-100 range, so the pivot stays
        # at the base high: the close at 82 is a rally inside the base.
        closes = [100, 90, 70, 75, 80, 77, 82, 90, 101]
        (base,) = detect_bases(bars(closes), RULES).bases
        assert (base.end, base.pivot) == (8, 100)
        # With the bar lowered to 30% of the range, 80 is a valid pivot and 82 breaks out.
        loose = replace(RULES, pivot_min_position=0.3)
        assert detect_bases(bars(closes), loose).forming is not None
        assert [b.end for b in detect_bases(bars(closes), loose).bases] == [6]

    def test_a_consolidation_shorter_than_the_minimum_is_ignored(self) -> None:
        closes = [100, 96, 93, 97, 101]  # 4 sessions, then above the pivot
        scan = detect_bases(bars(closes), RULES)
        assert scan.bases == ()
        assert scan.state.low_since_last == 93
        assert scan.state.high_since_last == 100

    def test_the_breakout_needs_a_close_above_the_pivot(self) -> None:
        # An intraday high above the pivot without a close above it doesn't end the base.
        closes = [100, 96, 92, 95, 99, 99, 101]
        data = bars(closes)
        data[5] = Bar(open=99, high=102, low=98, close=99.5, volume=1000)
        (base,) = detect_bases(data, RULES).bases
        assert base.end == 6
        assert base.high == 102

    def test_a_forming_base_is_reported_once_long_enough(self) -> None:
        closes = [80, 90, 100, 96, 92, 90, 93, 95]
        scan = detect_bases(bars(closes), RULES)
        assert scan.forming is not None
        assert scan.bases == (scan.forming,)
        assert scan.forming.status is BaseStatus.FORMING
        assert (scan.forming.start, scan.forming.end, scan.forming.sessions) == (2, None, 6)
        assert scan.forming.stage == 1
        short = detect_bases(bars(closes[:6]), RULES)
        assert short.bases == ()
        assert short.forming is not None
        assert short.forming.sessions == 4


def stages_path() -> list[float]:
    return [
        *seg(80, 100, 4),
        *base_path(100, 90, 99, 101),  # base 1: pivot 100, low 90
        *seg(101, 125, 4),
        *base_path(125, 112, 124, 126),  # +25% from 100: stage 2 (pivot 125)
        *seg(126, 140, 4),
        *base_path(140, 130, 139, 141),  # +12% from 125: base-on-base, stage 2 (pivot 140)
        *seg(141, 170, 4),
        *base_path(170, 155, 169, 171),  # +21% from 140: stage 3
    ]


class TestCounting:
    def test_stages_and_base_on_base(self) -> None:
        bases = detect_bases(bars([80, *stages_path()]), RULES).bases
        assert [b.pivot for b in bases] == [100, 125, 140, 170]
        assert [b.stage for b in bases] == [1, 2, 2, 3]
        assert [None if b.advance is None else round(b.advance, 4) for b in bases] == [
            None,
            0.25,
            0.12,
            round(170 / 140 - 1, 4),
        ]
        assert not any(b.count_reliable or b.reset for b in bases)

    def test_at_listing_the_count_is_reliable(self) -> None:
        bases = detect_bases(bars([80, *stages_path()]), RULES, BaseCountState.at_listing()).bases
        assert [b.stage for b in bases] == [1, 2, 2, 3]
        assert all(b.count_reliable for b in bases)
        assert not any(b.reset for b in bases)

    def test_the_advance_is_measured_from_the_last_real_base(self) -> None:
        # Between bases 1 and 2, a 3-session dip (ignored) peaks at 112 after the breakout; the
        # advance to base 2 still runs from base 1's pivot (100) to base 2's high (122): 22%.
        closes = [
            80,
            *seg(80, 100, 4),
            *base_path(100, 90, 99, 101),
            *seg(101, 112, 2),
            108,
            106,
            113,
            *seg(113, 122, 3),
            *base_path(122, 110, 121, 123),
        ]
        bases = detect_bases(bars(closes), RULES).bases
        assert [b.stage for b in bases] == [1, 2]
        assert bases[1].advance == pytest.approx(0.22)

    def test_a_run_up_inside_ignored_consolidations_counts_toward_the_advance(self) -> None:
        # The stock peaks at 121.5 inside a short consolidation, then forms base 2 from 118:
        # the advance is 21.5% (to 121.5), not 18% (to 118).
        closes = [
            80,
            *seg(80, 100, 4),
            *base_path(100, 90, 99, 101),
            *seg(101, 121, 4),
            116,
            121.5,
            118,
            *base_path(118, 108, 117.5, 122),
        ]
        bases = detect_bases(bars(closes), RULES).bases
        assert [b.stage for b in bases] == [1, 2]
        assert bases[1].advance == pytest.approx(0.215)

    def test_an_undercut_inside_the_base_resets_the_count(self) -> None:
        closes = [
            80,
            *seg(80, 100, 4),
            *base_path(100, 90, 99, 101),
            *seg(101, 125, 4),
            *base_path(125, 88, 124, 126),  # 88 < base 1's low of 90
        ]
        bases = detect_bases(bars(closes), RULES).bases
        assert [(b.stage, b.reset, b.count_reliable, b.advance) for b in bases] == [
            (1, False, False, None),
            (1, True, True, None),
        ]

    def test_an_equal_low_is_not_an_undercut(self) -> None:
        closes = [
            80,
            *seg(80, 100, 4),
            *base_path(100, 90, 99, 101),
            *seg(101, 125, 4),
            *base_path(125, 112, 124, 126),
        ]
        lows = {len(closes) - 6: 90.0}  # an intraday low exactly at base 1's low
        bases = detect_bases(bars(closes, lows), RULES).bases
        assert [(b.stage, b.reset) for b in bases] == [(1, False), (2, False)]

    def test_an_interim_low_between_bases_resets_the_count(self) -> None:
        # After base 1 breaks out at 101, a 3-session shakeout to 89 undercuts its low (90);
        # the higher base that follows starts the count again.
        closes = [
            80,
            *seg(80, 100, 4),
            *base_path(100, 90, 99, 101),
            95,
            89,
            95,
            102,
            *seg(102, 125, 4),
            *base_path(125, 112, 124, 126),
        ]
        bases = detect_bases(bars(closes), RULES).bases
        assert [(b.stage, b.reset, b.count_reliable) for b in bases] == [
            (1, False, False),
            (1, True, True),
        ]

    def test_an_intraday_undercut_counts(self) -> None:
        closes = [
            80,
            *seg(80, 100, 4),
            *base_path(100, 90, 99, 101),
            *seg(101, 125, 4),
            *base_path(125, 112, 124, 126),
        ]
        lows = {len(closes) - 6: 89.5}  # closes at 112, trades down to 89.5 intraday
        bases = detect_bases(bars(closes, lows), RULES).bases
        assert [(b.stage, b.reset) for b in bases] == [(1, False), (1, True)]

    def test_a_fast_crash_resets_without_a_row(self) -> None:
        closes = [
            80,
            *seg(80, 100, 4),
            *base_path(100, 90, 99, 101),
            *seg(101, 125, 4),
            100,
            80,
            61,  # 51% below 125 in 3 sessions: not a base, and too short for a row
            *seg(61, 75, 3),
            *base_path(75, 68, 74, 76),
        ]
        scan = detect_bases(bars(closes), RULES)
        assert [b.status for b in scan.bases] == [BaseStatus.BROKEN_OUT, BaseStatus.BROKEN_OUT]
        assert [(b.stage, b.reset, b.count_reliable) for b in scan.bases] == [
            (1, False, False),
            (1, True, True),
        ]

    def test_a_slow_decline_past_the_maximum_depth_is_a_failed_row(self) -> None:
        closes = [
            80,
            *seg(80, 100, 4),
            *base_path(100, 90, 99, 101),
            *seg(101, 125, 4),
            *seg(125, 60, 10),  # fails at the first close below 62.5 (session 10 of the decline)
            *seg(60, 75, 3),
            *base_path(75, 68, 74, 76),
        ]
        scan = detect_bases(bars(closes), RULES)
        assert [b.status for b in scan.bases] == [
            BaseStatus.BROKEN_OUT,
            BaseStatus.FAILED,
            BaseStatus.BROKEN_OUT,
        ]
        failed = scan.bases[1]
        assert failed.stage is None
        assert failed.depth > RULES.max_depth
        assert failed.end is not None
        assert failed.sessions == failed.end - failed.start == 10
        assert (scan.bases[2].stage, scan.bases[2].reset, scan.bases[2].count_reliable) == (
            1,
            True,
            True,
        )

    def test_after_a_failure_even_another_failure_is_reliable(self) -> None:
        closes = [100, *seg(100, 45, 6), *seg(45, 20, 6), *seg(20, 30, 3)]
        scan = detect_bases(bars(closes), RULES)
        assert [(b.status, b.count_reliable) for b in scan.bases] == [
            (BaseStatus.FAILED, False),
            (BaseStatus.FAILED, True),
        ]
        assert scan.state.count_reliable
        assert scan.state.reset_pending

    def test_a_reset_makes_later_counts_reliable(self) -> None:
        closes = [
            80,
            *seg(80, 100, 4),
            *base_path(100, 90, 99, 101),
            *seg(101, 125, 4),
            *base_path(125, 88, 124, 126),  # reset
            *seg(126, 155, 4),
            *base_path(155, 140, 154, 156),  # +24% from the reset base's pivot (125): stage 2
        ]
        bases = detect_bases(bars(closes), RULES).bases
        assert [(b.stage, b.count_reliable) for b in bases] == [(1, False), (1, True), (2, True)]

    def test_the_forming_base_gets_its_prospective_stage(self) -> None:
        closes = [
            80,
            *seg(80, 100, 4),
            *base_path(100, 90, 99, 101),
            *seg(101, 125, 4),
            *seg(125, 112, 4),
            *seg(112, 120, 2),
        ]
        scan = detect_bases(bars(closes), RULES)
        assert scan.forming is not None
        assert scan.forming.status is BaseStatus.FORMING
        assert (scan.forming.stage, scan.forming.advance) == (2, pytest.approx(0.25))
        assert scan.bases[-1] == scan.forming


class TestCarriedState:
    def test_after_needs_a_base_that_broke_out(self) -> None:
        forming = detect_bases(bars([100, 96, 92, 95, 96, 97]), RULES).forming
        assert forming is not None
        with pytest.raises(ValueError, match="broke out"):
            BaseCountState.after(forming)

    def test_carrying_the_state_matches_one_pass(self) -> None:
        data = bars([80, *stages_path()])
        full = detect_bases(data, RULES).bases
        for k, base in enumerate(full):
            assert base.end is not None
            rest = detect_bases(data[base.end :], RULES, BaseCountState.after(base)).bases
            shifted = [_shift(b, base.end) for b in rest]
            assert shifted == list(full[k + 1 :])


def _shift(base: PriceBase, by: int) -> PriceBase:
    return replace(
        base,
        start=base.start + by,
        end=None if base.end is None else base.end + by,
        low_session=base.low_session + by,
        contractions=tuple(
            replace(c, high_session=c.high_session + by, low_session=c.low_session + by)
            for c in base.contractions
        ),
    )


@st.composite
def random_bars(draw: st.DrawFn) -> list[Bar]:
    steps = draw(st.lists(st.floats(-0.06, 0.06), min_size=1, max_size=250))
    spreads = draw(st.lists(st.floats(0, 0.04), min_size=len(steps), max_size=len(steps)))
    out, close = [], 100.0
    for step, spread in zip(steps, spreads, strict=True):
        close *= 1 + step
        out.append(
            Bar(
                open=close,
                high=close * (1 + spread),
                low=close * (1 - spread),
                close=close,
                volume=1,
            )
        )
    return out


class TestProperties:
    @settings(max_examples=200, deadline=None)
    @given(random_bars(), st.integers(1, 250))
    def test_completed_bases_never_change_with_more_history(self, data: list[Bar], k: int) -> None:
        k = min(k, len(data))
        full = detect_bases(data, RULES).bases
        prefix = detect_bases(data[:k], RULES).bases
        assert [b for b in prefix if b.end is not None] == [
            b for b in full if b.end is not None and b.end < k
        ]

    @settings(max_examples=200, deadline=None)
    @given(random_bars())
    def test_invariants(self, data: list[Bar]) -> None:
        scan = detect_bases(data, RULES)
        for base in scan.bases:
            assert base.sessions >= RULES.min_sessions
            assert base.low <= base.pivot <= base.high
            assert (base.stage is None) == (base.status is BaseStatus.FAILED)
            if base.status is not BaseStatus.FAILED:
                assert base.depth <= RULES.max_depth
                assert base.stage is not None
                assert base.stage >= 1
            if base.reset:
                assert base.stage == 1
                assert base.count_reliable
            for c in base.contractions:
                assert base.start <= c.high_session <= c.low_session
                assert c.depth >= RULES.swing_reversal - 1e-12
        ends = [b.end for b in scan.bases if b.end is not None]
        assert ends == sorted(ends)

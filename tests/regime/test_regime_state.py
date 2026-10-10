"""The regime state machine: every row of the README §5.2 transition table, plus the edge cases
of plan §2.3. Scores are given directly; bars drive distribution days, rally attempts and FTDs.
"""

from dataclasses import replace

from auratrade.config import DEFAULT_RISK_CONFIG, Regime, load_risk_config
from auratrade.indicators.bars import Bar
from auratrade.regime.state import RegimeState, Tracking, advance, early_uptrend_stepped, track

CFG = load_risk_config(DEFAULT_RISK_CONFIG).regime
CU, UP, EU, CORR = (
    Regime.CONFIRMED_UPTREND,
    Regime.UNDER_PRESSURE,
    Regime.EARLY_UPTREND,
    Regime.CORRECTION,
)

type Print = tuple[float, float, float]  # close, low, volume


class Machine:
    """Steps the machine one session at a time. Each index repeats its last close (flat, same
    volume, low = close) unless a print is given."""

    def __init__(self, state: RegimeState | None = None, *, expiration: bool = False) -> None:
        self.state = state
        self.expiration = expiration
        self.prev = {"SPY": Bar(100, 100, 100, 100, 1000), "QQQ": Bar(100, 100, 100, 100, 1000)}
        self.tracking: Tracking | None = None

    def step(self, score: int = 0, **prints: Print) -> RegimeState:
        bars = {}
        for index, prev in self.prev.items():
            close, low, volume = prints.get(index, (prev.close, prev.close, prev.volume))
            bars[index] = (prev, Bar(close, max(close, prev.close), low, close, volume))
        self.tracking = track(self.state, bars, expiration_session=self.expiration, cfg=CFG)
        self.state = advance(self.state, self.tracking, bars, score, CFG)
        self.prev = {i: bar for i, (_, bar) in bars.items()}
        return self.state

    def steps(self, n: int, score: int = 0) -> RegimeState:
        for _ in range(n):
            self.step(score)
        assert self.state is not None
        return self.state


def p(close: float, volume: float = 1000, low: float | None = None) -> Print:
    return (close, close if low is None else low, volume)


def in_state(regime: Regime) -> Machine:
    """A machine already in ``regime`` (entered at seq 0) with flat index bars at 100."""
    m = Machine(RegimeState(regime=regime, seq=0, entered_seq=0, rally_lows={"SPY": 90, "QQQ": 90}))
    if regime is CORR:
        m = Machine()
        m.step()
    return m


def after_ftd() -> Machine:
    """CORRECTION from seq 0 (lows 100), then SPY day 1-3 and a +1.47% day 4 on higher
    volume at seq 4: the FTD. ``EARLY_UPTREND`` entered at seq 4, rally lows 100 / 100."""
    m = Machine()
    m.step()
    for close in (101, 101.5, 102):
        m.step(-3, SPY=p(close))
    state = m.step(-3, SPY=p(103.5, 2000))
    assert state.regime is EU
    assert state.entered_seq == 4
    assert not state.no_ftd
    return m


class TestStart:
    def test_the_machine_starts_in_correction_with_that_session_as_the_low(self) -> None:
        m = Machine()
        state = m.step(5, SPY=p(100, low=98))
        assert state.regime is CORR
        assert (state.seq, state.entered_seq, state.sessions_in_state) == (0, 0, 1)
        assert state.rally["SPY"].rally_low == 98
        assert state.rally["SPY"].day == 0


class TestConfirmedUptrend:
    def test_score_at_or_below_minus_one_is_an_immediate_correction(self) -> None:
        assert in_state(CU).step(-1).regime is CORR

    def test_score_at_or_below_plus_one_is_an_immediate_downgrade(self) -> None:
        assert in_state(CU).step(1).regime is UP
        assert in_state(CU).step(0).regime is UP

    def test_plus_two_needs_two_sessions_in_a_row(self) -> None:
        m = in_state(CU)
        assert m.step(2).regime is CU
        assert m.step(2).regime is UP

    def test_a_single_pillar_flickering_across_a_threshold_does_not_flip_it(self) -> None:
        m = in_state(CU)
        for score in (2, 3, 2, 3, 2, 3, 2):
            assert m.step(score).regime is CU
        assert m.state is not None
        assert m.state.debounce_streak == 1

    def test_entering_resets_the_counters(self) -> None:
        m = in_state(UP)
        m.steps(3, score=3)
        assert m.state is not None
        assert m.state.regime is CU
        assert (m.state.upgrade_streak, m.state.debounce_streak) == (0, 0)


class TestUnderPressure:
    def test_score_at_or_below_minus_one_is_an_immediate_correction(self) -> None:
        assert in_state(UP).step(-2).regime is CORR

    def test_three_sessions_at_plus_three_upgrade(self) -> None:
        m = in_state(UP)
        assert m.step(3).regime is UP
        assert m.step(4).regime is UP
        assert m.step(5).regime is CU

    def test_a_break_restarts_the_upgrade_count(self) -> None:
        m = in_state(UP)
        for score in (3, 3, 2, 3, 3):
            assert m.step(score).regime is UP
        assert m.step(3).regime is CU

    def test_upgrade_sessions_count_only_in_the_current_state(self) -> None:
        # Two +3 sessions in CONFIRMED_UPTREND, then a +1 drops it: the count starts again.
        m = in_state(CU)
        m.step(3)
        m.step(3)
        assert m.step(1).regime is UP
        assert m.step(3).regime is UP
        assert m.step(3).regime is UP
        assert m.step(3).regime is CU


class TestFollowThrough:
    def test_an_ftd_on_day_four_starts_an_early_uptrend(self) -> None:
        m = after_ftd()
        assert m.state is not None
        assert m.state.rally_lows == {"SPY": 100, "QQQ": 100}
        assert m.state.last_ftd_seq == 4

    def test_day_three_is_too_early(self) -> None:
        m = Machine()
        m.step()
        m.step(SPY=p(101))
        m.step(SPY=p(101.5))
        assert m.step(SPY=p(103.5, 2000)).regime is CORR  # +2% on day 3

    def test_an_ftd_needs_higher_volume(self) -> None:
        m = Machine()
        m.step()
        for close in (101, 101.5, 102):
            m.step(SPY=p(close))
        assert m.step(SPY=p(103.5, 900)).regime is CORR

    def test_a_new_low_restarts_the_count(self) -> None:
        m = Machine()
        m.step()
        m.step(SPY=p(101))
        m.step(SPY=p(101.5))
        m.step(SPY=p(99, low=98))  # below the correction low of 100: day 0 again
        assert m.state is not None
        assert m.state.rally["SPY"].rally_low == 98
        for close in (100, 100.5, 101):
            assert m.step(SPY=p(close)).regime is CORR
        assert m.step(SPY=p(102.5, 2000)).regime is EU

    def test_an_undercut_by_either_index_restarts_both_counts(self) -> None:
        m = Machine()
        m.step()
        m.step(SPY=p(101))
        m.step(SPY=p(101.5))
        m.step(SPY=p(102), QQQ=p(99.5, low=99))  # QQQ undercuts its low; SPY's day 3 is lost
        assert m.state is not None
        assert (m.state.rally["SPY"].day, m.state.rally["SPY"].rally_low) == (0, 100)
        assert m.state.rally["QQQ"].rally_low == 99
        assert m.step(SPY=p(104, 2000)).regime is CORR  # would have been day 4

    def test_a_correction_entered_on_the_score_near_the_high(self) -> None:
        # The correction low is the entry session's low, and an FTD can come from day 4.
        m = in_state(CU)
        state = m.step(-1, SPY=p(99, low=98.5))
        assert state.regime is CORR
        assert state.rally["SPY"].rally_low == 98.5
        assert state.rally["QQQ"].rally_low == 100
        for close in (99.5, 100, 100.5):
            assert m.step(-2, SPY=p(close)).regime is CORR
        assert m.step(-2, SPY=p(102, 2000)).regime is EU

    def test_an_ftd_on_either_index(self) -> None:
        m = Machine()
        m.step()
        for close in (101, 101.5, 102):
            m.step(QQQ=p(close))
        state = m.step(QQQ=p(103.5, 2000))
        assert state.regime is EU
        assert m.tracking is not None
        assert m.tracking.follow_through is not None
        assert m.tracking.follow_through.index == "QQQ"


class TestNoFtdFallback:
    def test_ten_sessions_at_plus_three_without_an_ftd(self) -> None:
        m = Machine()
        m.step()
        m.step(3, SPY=p(100, low=97))
        m.step(3, QQQ=p(100, low=98.5))
        for _ in range(7):
            assert m.step(3).regime is CORR
        state = m.step(3)
        assert state.regime is EU
        assert state.no_ftd
        assert state.entered_seq == 10
        # The rally low is each index's lowest low over the 10 qualifying sessions.
        assert state.rally_lows == {"SPY": 97, "QQQ": 98.5}

    def test_a_lower_score_restarts_the_count(self) -> None:
        m = Machine()
        m.step()
        m.steps(9, score=3)
        m.step(2)
        assert m.steps(9, score=3).regime is CORR
        assert m.step(3).regime is EU

    def test_the_fallback_firing_one_session_before_an_ftd_would_have(self) -> None:
        m = Machine()
        m.step()
        m.steps(6, score=3)
        m.step(3, SPY=p(99, low=98))  # seq 7: a new low
        m.step(3, SPY=p(100))  # day 1
        m.step(3, SPY=p(100.5))  # day 2
        state = m.step(3, SPY=p(101))  # seq 10, day 3: the 10th session at +3
        assert state.regime is EU
        assert state.no_ftd
        assert state.rally_lows["SPY"] == 98

    def test_an_ftd_on_the_fallback_session_wins(self) -> None:
        m = Machine()
        m.step()
        m.steps(5, score=3)
        m.step(3, SPY=p(99, low=98))  # seq 6: a new low
        for close in (100, 100.5, 101):  # days 1-3, seq 7-9
            m.step(3, SPY=p(close))
        state = m.step(3, SPY=p(102.5, 2000))  # seq 10: the FTD and the 10th session at +3
        assert state.regime is EU
        assert not state.no_ftd
        assert state.rally_lows["SPY"] == 98

    def test_no_ftd_then_an_undercut_of_the_ten_session_low(self) -> None:
        m = Machine()
        m.step()
        m.step(3, SPY=p(100, low=97))
        m.steps(8, score=3)
        assert m.step(3).regime is EU
        assert m.step(3, SPY=p(100, low=97)).regime is EU  # equal is not an undercut
        assert m.step(3, SPY=p(100, low=96.9)).regime is CORR

    def test_no_ftd_at_plus_three_waits_its_ten_sessions(self) -> None:
        m = Machine()
        m.step()
        assert m.steps(10, score=3).regime is EU
        for _ in range(9):  # labels 2-10 in EARLY_UPTREND
            assert m.step(3).regime is EU
        state = m.step(3)
        assert state.regime is CU
        assert state.seq == 20


class TestEarlyUptrend:
    def test_a_negative_score_does_not_end_it(self) -> None:
        m = after_ftd()
        for _ in range(15):
            assert m.step(-3).regime is EU

    def test_a_stalled_early_uptrend_stays_put(self) -> None:
        m = after_ftd()
        for k in range(45):
            assert m.step(1 + k % 2).regime is EU

    def test_an_undercut_of_either_rally_low_ends_it(self) -> None:
        m = after_ftd()
        assert m.step(3, QQQ=p(100, low=99.99)).regime is CORR
        assert m.state is not None
        assert m.state.rally["QQQ"].rally_low == 99.99  # a new correction from this session

    def test_six_distribution_days_since_entering_end_it(self) -> None:
        m = after_ftd()
        m.step(QQQ=p(110))
        for k in range(6):
            state = m.step(2, QQQ=p(109.7, 1100))  # -0.27% on higher volume
            if k < 5:
                assert state.regime is EU
                m.step(2, QQQ=p(110, 1000))
        assert state.regime is CORR

    def test_distribution_days_before_entering_do_not_count(self) -> None:
        # On the no_ftd path nothing resets the counts: two QQQ distribution days during the
        # fallback streak, four after entering. Six in the window, four since entering.
        m = Machine()
        m.step()
        m.step(3, QQQ=p(110))
        for _ in range(2):
            m.step(3, QQQ=p(109.7, 1100))
            m.step(3, QQQ=p(110, 1000))
        state = m.steps(5, score=3)
        assert state.regime is EU
        assert state.no_ftd
        for _ in range(4):
            m.step(3, QQQ=p(109.7, 1100))
            assert m.step(1, QQQ=p(110, 1000)).regime is EU
        assert m.tracking is not None
        assert m.tracking.distribution_count()["QQQ"] == 6

    def test_the_upgrade_needs_ten_labels_in_the_state(self) -> None:
        m = after_ftd()  # entered at seq 4
        for _ in range(9):
            assert m.step(3).regime is EU
        state = m.step(3)  # seq 14: 10 earlier labels in EARLY_UPTREND
        assert state.regime is CU
        assert state.seq == 14

    def test_the_upgrade_still_needs_three_sessions_at_plus_three(self) -> None:
        m = after_ftd()
        m.steps(12, score=1)
        assert m.step(3).regime is EU
        assert m.step(3).regime is EU
        assert m.step(3).regime is CU

    def test_a_rally_low_undercut_on_the_day_of_an_upgrade(self) -> None:
        m = after_ftd()
        for _ in range(9):
            m.step(3)
        assert m.step(3, SPY=p(100, low=99)).regime is CORR

    def test_the_step_up_after_ten_sessions(self) -> None:
        kwargs = {"starter_sessions": 10, "min_score": 1}
        m = after_ftd()
        m.steps(9, score=0)
        assert m.state is not None
        assert not early_uptrend_stepped(m.state, 5, **kwargs)  # label 10: still starter
        state = m.step(0)
        assert not early_uptrend_stepped(state, 0, **kwargs)
        assert early_uptrend_stepped(state, 1, **kwargs)
        assert not early_uptrend_stepped(replace(state, no_ftd=True), 3, **kwargs)
        assert not early_uptrend_stepped(replace(state, regime=CU), 3, **kwargs)


class TestDistributionTracking:
    def test_counts_reset_after_an_ftd(self) -> None:
        m = Machine()
        m.step()
        m.step(QQQ=p(110))
        m.step(QQQ=p(109.7, 1100))
        assert m.tracking is not None
        assert m.tracking.distribution_count() == {"SPY": 0, "QQQ": 1}
        for close in (101, 101.5, 102):
            m.step(SPY=p(close))
        m.step(SPY=p(103.5, 2000))
        assert m.tracking.distribution_count() == {"SPY": 0, "QQQ": 0}

    def test_the_window_is_twenty_five_sessions(self) -> None:
        m = in_state(UP)
        m.step(QQQ=p(99.7, 1100))  # seq 1
        m.steps(24)
        assert m.tracking is not None
        assert m.tracking.distribution_count()["QQQ"] == 1  # seq 25: sessions 1-25
        m.step()
        assert m.tracking.distribution_count()["QQQ"] == 0

    def test_expiration_sessions_are_counted_unless_excluded(self) -> None:
        m = Machine(RegimeState(regime=UP, seq=0, entered_seq=0), expiration=True)
        m.step(QQQ=p(99.7, 1100))
        assert m.tracking is not None
        assert m.tracking.distribution_count()["QQQ"] == 1
        excluded = CFG.model_copy(
            update={
                "distribution": CFG.distribution.model_copy(
                    update={"exclude_expiration_sessions": True}
                )
            }
        )
        bars = {
            "SPY": (Bar(100, 100, 100, 100, 1000), Bar(100, 100, 100, 100, 1000)),
            "QQQ": (Bar(100, 100, 100, 100, 1000), Bar(99.7, 100, 99.7, 99.7, 1100)),
        }
        tracking = track(
            RegimeState(regime=UP, seq=0, entered_seq=0),
            bars,
            expiration_session=True,
            cfg=excluded,
        )
        assert tracking.distribution_today["QQQ"]
        assert tracking.distribution_count()["QQQ"] == 0


def test_state_round_trips_through_json() -> None:
    m = Machine()
    m.step()
    m.step(QQQ=p(99.7, 1100, low=99))
    m.step(3, SPY=p(101))
    assert m.state is not None
    assert RegimeState.from_json(m.state.to_json()) == m.state
    eu = after_ftd().state
    assert eu is not None
    assert RegimeState.from_json(eu.to_json()) == eu

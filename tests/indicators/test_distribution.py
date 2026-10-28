import pytest

from auratrade.indicators.bars import Bar
from auratrade.indicators.distribution import (
    RallyAttempt,
    distribution_count,
    distribution_days,
    start_rally_attempt,
    step_rally_attempt,
)

FTD_RULES = {"min_gain": 0.0125, "first_day": 4, "last_strong_day": 7}


def bar(close: float, volume: float = 1000, low: float | None = None) -> Bar:
    return Bar(
        open=close, high=close, low=close if low is None else low, close=close, volume=volume
    )


def test_distribution_days() -> None:
    series = [
        bar(100, 1000),
        bar(99.8, 1100),  # exactly -0.2% on higher volume
        bar(99.7, 1000),  # down 0.1% on lower volume
        bar(98, 1200),  # down 1.7% on higher volume
        bar(97, 1100),  # down on lower volume
        bar(98, 2000),  # up on higher volume
        bar(97.9, 2100),  # down 0.1% on higher volume: too small
    ]
    assert distribution_days(series, min_decline=0.002) == [
        False,
        True,
        False,
        True,
        False,
        False,
        False,
    ]
    assert distribution_days([], min_decline=0.002) == []


def test_distribution_count_window_reset_and_exclusions() -> None:
    flags = [True, False, True, True, False, True]
    assert distribution_count(flags, 5, window=4) == 3  # sessions 2-5
    assert distribution_count(flags, 5, window=25) == 4
    assert distribution_count(flags, 5, window=4, reset_after=3) == 1  # FTD at 3: sessions 4-5
    assert distribution_count(flags, 3, window=4, reset_after=3) == 0  # the FTD session itself
    excluded = [False] * 5 + [True]  # e.g. a quarterly option-expiration session
    assert distribution_count(flags, 5, window=4, excluded=excluded) == 2
    with pytest.raises(IndexError):
        distribution_count(flags, 6, window=4)


# Correction entered at session 0. Session 1 undercuts (new correction low 96). Session 2 is the
# first up close (day 1). Session 4 gains 2% on day 3: too early. Session 5 gains 1.5% on higher
# volume on day 4: the follow-through day.
RALLY = [
    bar(100, 1000, low=99),
    bar(97, 1000, low=96),
    bar(98, 900, low=97),
    bar(97.5, 900, low=97),
    bar(99.5, 1200),
    bar(101, 1300),
]


def test_new_low_restarts_the_count() -> None:
    state = RallyAttempt(rally_low=96, low_session=1, day=2)
    state, ftd = step_rally_attempt(state, RALLY[3], bar(99, 5000, low=95.9), 4, **FTD_RULES)
    assert state == RallyAttempt(rally_low=95.9, low_session=4, day=0)
    assert ftd is None


def test_touching_the_rally_low_is_not_an_undercut() -> None:
    state = RallyAttempt(rally_low=96, low_session=1, day=2)
    state, _ = step_rally_attempt(state, RALLY[3], bar(97, 900, low=96), 4, **FTD_RULES)
    assert state == RallyAttempt(rally_low=96, low_session=1, day=3)


def test_the_low_session_is_never_day_one() -> None:
    state = start_rally_attempt(bar(100, low=99), 0)
    # New low, but closes above the prior close: still waiting for day 1.
    state, _ = step_rally_attempt(state, bar(100, low=99), bar(100.5, low=98.5), 1, **FTD_RULES)
    assert state.day == 0
    state, _ = step_rally_attempt(state, bar(100.5), bar(101, low=99), 2, **FTD_RULES)
    assert state.day == 1


def test_down_closes_before_day_one_keep_waiting() -> None:
    state = start_rally_attempt(bar(100, low=99), 0)
    state, _ = step_rally_attempt(state, bar(100), bar(99.5, low=99.2), 1, **FTD_RULES)
    assert state == RallyAttempt(rally_low=99, low_session=0, day=0)

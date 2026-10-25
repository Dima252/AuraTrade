from datetime import date

import pytest

from auratrade.db.models import EARNINGS_TIMINGS
from auratrade.plans.cushion import (
    CushionAction,
    EarningsTiming,
    cushion_action,
    cushion_session,
    estimated_cushion_session,
)
from tests.plans.factories import PLANS, RISK

# Mon 2026-10-05 .. Fri 2026-10-16, no holidays: ten sessions.
WEEK1 = [date(2026, 10, d) for d in range(5, 10)]
WEEK2 = [date(2026, 10, d) for d in range(12, 17)]
SESSIONS = WEEK1 + WEEK2


def test_timings_match_the_database() -> None:
    assert tuple(EarningsTiming) == EARNINGS_TIMINGS


@pytest.mark.parametrize(
    ("report", "timing", "session"),
    [
        (date(2026, 10, 14), EarningsTiming.BEFORE_OPEN, date(2026, 10, 13)),
        (date(2026, 10, 14), EarningsTiming.AFTER_CLOSE, date(2026, 10, 14)),
        # Monday before the open: Friday's review.
        (date(2026, 10, 12), EarningsTiming.BEFORE_OPEN, date(2026, 10, 9)),
        # During the market or unknown timing: a session early, never a session late.
        (date(2026, 10, 14), EarningsTiming.DURING_MARKET, date(2026, 10, 13)),
        (date(2026, 10, 14), EarningsTiming.UNKNOWN, date(2026, 10, 13)),
        # A report dated on a non-session day (Saturday) after the close: Friday.
        (date(2026, 10, 10), EarningsTiming.AFTER_CLOSE, date(2026, 10, 9)),
    ],
)
def test_cushion_session(report: date, timing: EarningsTiming, session: date) -> None:
    assert cushion_session(report, timing, SESSIONS) == session


def test_cushion_session_before_the_calendar() -> None:
    assert cushion_session(date(2026, 10, 5), EarningsTiming.BEFORE_OPEN, SESSIONS) is None


def test_estimated_cushion_session_counts_sessions() -> None:
    # Estimate = last report + 6 sessions, window ±2: the window starts at +4, so the cushion
    # runs at +3 sessions.
    session = estimated_cushion_session(
        date(2026, 10, 7), SESSIONS, estimate_sessions=6, window_sessions=2
    )
    assert session == date(2026, 10, 12)  # Wed + 3 sessions, across the weekend
    # A report on a non-session day counts from the next session.
    assert estimated_cushion_session(
        date(2026, 10, 10), SESSIONS, estimate_sessions=6, window_sessions=2
    ) == date(2026, 10, 15)


def test_estimated_cushion_session_with_the_repo_config() -> None:
    # 70 consecutive dates stand in for a calendar: only their order matters.
    sessions = [date.fromordinal(date(2026, 1, 2).toordinal() + i) for i in range(70)]
    guard = RISK.guardrails
    session = estimated_cushion_session(
        sessions[0],
        sessions,
        estimate_sessions=guard.earnings_estimate_sessions,
        window_sessions=guard.earnings_estimate_window_sessions,
    )
    assert session == sessions[52]  # the window starts at +53 (archived README §7.4)


def test_estimated_cushion_session_beyond_the_calendar() -> None:
    assert (
        estimated_cushion_session(
            date(2026, 10, 14), SESSIONS, estimate_sessions=6, window_sessions=2
        )
        is None
    )
    with pytest.raises(ValueError, match="window"):
        estimated_cushion_session(SESSIONS[0], SESSIONS, estimate_sessions=5, window_sessions=5)


@pytest.mark.parametrize(
    ("gain_r", "action"),
    [
        (-0.5, CushionAction.EXIT),
        (0.99, CushionAction.EXIT),
        (1.0, CushionAction.TRIM),
        (2.99, CushionAction.TRIM),
        (3.0, CushionAction.HOLD),
        (7.0, CushionAction.HOLD),
    ],
)
def test_cushion_table(gain_r: float, action: CushionAction) -> None:
    assert cushion_action(gain_r, PLANS.earnings_cushion) is action


def test_cushion_rejects_a_non_finite_gain() -> None:
    with pytest.raises(ValueError, match="finite"):
        cushion_action(float("nan"), PLANS.earnings_cushion)

"""The weekday schedule (`jobs.schedule`): slot times, the chain's fallbacks and the heartbeats
the watchdog expects."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise

import pytest

from auratrade.jobs import schedule
from auratrade.jobs.schedule import NY, Close, Expected, Heartbeat, Slot

DAY = date(2026, 10, 22)
CHAIN_WAIT = 30
GRACE = 15


def at(hour: int, minute: int = 0) -> datetime:
    return datetime.combine(DAY, time(hour, minute), tzinfo=NY)


def slots_for(job: str) -> list[Slot]:
    return [s for s in schedule.skeleton_slots(CHAIN_WAIT) if s.job == job]


def test_a_single_slot_fires_once() -> None:
    assert Slot("execute_entries", time(9, 36)).times() == [time(9, 36)]
    assert Slot("execute_entries", time(9, 36)).last == time(9, 36)


def test_a_repeating_slot_includes_its_end() -> None:
    slot = Slot("x", time(17, 0), time(17, 25), 10)
    assert slot.times() == [time(17, 0), time(17, 10), time(17, 20), time(17, 25)]
    assert slot.last == time(17, 25)
    assert Slot("x", time(17, 0), time(17, 20), 10).times() == [
        time(17, 0),
        time(17, 10),
        time(17, 20),
    ]


@pytest.mark.parametrize(
    ("end", "every", "match"),
    [
        (time(18, 0), 0, "both end and every_minutes"),
        (None, 10, "both end and every_minutes"),
        (time(17, 0), 10, "not after start"),
        (time(16, 0), 10, "not after start"),
    ],
)
def test_bad_slots_are_refused(end: time | None, every: int, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        Slot("x", time(17, 0), end, every)


@pytest.mark.parametrize(
    ("close", "regular", "early"),
    [(Close.ANY, True, True), (Close.REGULAR, True, False), (Close.EARLY, False, True)],
)
def test_slots_apply_by_the_kind_of_close(close: Close, regular: bool, early: bool) -> None:
    slot = Slot("x", time(12, 40), close=close)
    assert slot.applies_to(early_close=False) is regular
    assert slot.applies_to(early_close=True) is early


def test_the_day_schedule() -> None:
    assert [s.start for s in slots_for("execute_open")] == [time(9, 20), time(9, 31)]
    assert [s.start for s in slots_for("execute_entries")] == [time(9, 36)]
    (guard,) = slots_for("stop_guard")
    times = guard.times()
    assert (times[0], times[-1], len(times)) == (time(9, 35), time(16, 0), 78)
    assert guard.heartbeat is Heartbeat.NONE
    (watchdog,) = slots_for("watchdog")
    assert (watchdog.start, watchdog.last, len(watchdog.times())) == (time(7), time(22), 61)
    assert watchdog.heartbeat is Heartbeat.NONE
    assert [(s.start, s.close) for s in slots_for("eod_review")] == [
        (time(12, 40), Close.EARLY),
        (time(15, 40), Close.REGULAR),
    ]


def test_the_chain_head_fires_once_and_the_rest_fall_back_until_their_deadline() -> None:
    (head,) = slots_for("post_market")
    assert (head.times(), head.heartbeat) == ([time(16, 20)], Heartbeat.AT_START)
    for link in schedule.EVENING_CHAIN[1:]:
        (slot,) = slots_for(link.job)
        times = slot.times()
        assert times[0] == link.fallback_start
        assert slot.heartbeat is Heartbeat.BY_END
        deadline = datetime.combine(DAY, link.fallback_start) + timedelta(minutes=CHAIN_WAIT)
        assert times[-1] == deadline.time()
        gaps = {
            (datetime.combine(DAY, b) - datetime.combine(DAY, a)).seconds // 60
            for a, b in pairwise(times)
        }
        assert max(gaps) <= schedule.FALLBACK_EVERY_MINUTES
    (ingest,) = slots_for("ingest")
    assert ingest.times() == [time(17, 0), time(17, 10), time(17, 20), time(17, 30)]


def test_every_skeleton_job_is_scheduled() -> None:
    jobs = {s.job for s in schedule.skeleton_slots(CHAIN_WAIT)}
    chain = {link.job for link in schedule.EVENING_CHAIN}
    assert jobs == chain | {
        "watchdog",
        "execute_open",
        "execute_entries",
        "stop_guard",
        "eod_review",
    }


def test_expected_heartbeats_on_a_regular_day() -> None:
    expected = schedule.expected_heartbeats(
        schedule.skeleton_slots(CHAIN_WAIT), DAY, early_close=False, grace_minutes=GRACE
    )
    assert [e.label for e in expected] == [
        "execute_open 09:20",
        "execute_open 09:31",
        "execute_entries 09:36",
        "eod_review 15:40",
        "post_market 16:20",
        "ingest by 17:45",
        "simulate by 18:00",
        "regime by 18:10",
        "scan by 18:20",
        "analyst_submit by 18:35",
    ]
    assert expected[0] == Expected("execute_open", at(9, 20), at(9, 35))
    assert expected[5] == Expected("ingest", None, at(17, 45))
    assert [e.due for e in expected] == sorted(e.due for e in expected)


def test_a_half_day_expects_the_early_eod_review_instead() -> None:
    expected = schedule.expected_heartbeats(
        schedule.skeleton_slots(CHAIN_WAIT), DAY, early_close=True, grace_minutes=GRACE
    )
    reviews = [e for e in expected if e.job == "eod_review"]
    assert reviews == [Expected("eod_review", at(12, 40), at(12, 55))]
    assert not any(e.job in ("stop_guard", "watchdog") for e in expected)


def test_labels_are_in_new_york_time() -> None:
    utc = at(9, 20).astimezone(UTC)
    assert Expected("execute_open", utc, utc).label == "execute_open 09:20"
    assert Expected("ingest", None, utc).label == "ingest by 09:20"

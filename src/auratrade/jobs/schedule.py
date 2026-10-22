"""The job schedule (README §2, §3), all America/New_York, weekdays.

The single source for the runner (the evening chain), the scheduler stack (task 1.8), the
`watchdog`'s expected heartbeats and the local stand-in `scripts/local_session.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class ChainLink:
    job: str
    # The scheduled start. For every job after the first it is a fallback: the job normally
    # starts when its predecessor succeeds and invokes it.
    fallback_start: time


EVENING_CHAIN: tuple[ChainLink, ...] = (
    ChainLink("post_market", time(16, 20)),
    ChainLink("ingest", time(17, 0)),
    ChainLink("simulate", time(17, 15)),
    ChainLink("regime", time(17, 25)),
    ChainLink("scan", time(17, 35)),
    ChainLink("analyst_submit", time(17, 50)),
)

_INDEX = {link.job: i for i, link in enumerate(EVENING_CHAIN)}


def chain_link(job: str) -> ChainLink | None:
    i = _INDEX.get(job)
    return None if i is None else EVENING_CHAIN[i]


def predecessor(job: str) -> str | None:
    i = _INDEX.get(job)
    return None if i is None or i == 0 else EVENING_CHAIN[i - 1].job


def successor(job: str) -> str | None:
    i = _INDEX.get(job)
    return None if i is None or i == len(EVENING_CHAIN) - 1 else EVENING_CHAIN[i + 1].job


# --- the skeleton's weekday schedule -----------------------------------------------------


class Close(StrEnum):
    """Which sessions a slot belongs to. A scheduler can't follow the calendar, so both of
    `eod_review`'s slots fire every weekday and the job runs only near that day's close."""

    ANY = "any"
    REGULAR = "regular"
    EARLY = "early"


class Heartbeat(StrEnum):
    """What the `watchdog` expects from a slot on a session day."""

    AT_START = "at_start"  # a run that started at or after the slot's start
    BY_END = "by_end"  # a run for the session, by the slot's last time (the chain's fallbacks)
    NONE = "none"  # quiet jobs (`stop_guard`) and the watchdog itself


@dataclass(frozen=True)
class Slot:
    """One job's runs: once at `start`, or every `every_minutes` from `start` through `end`
    (`end` itself always included)."""

    job: str
    start: time
    end: time | None = None
    every_minutes: int = 0
    close: Close = Close.ANY
    heartbeat: Heartbeat = Heartbeat.AT_START

    def __post_init__(self) -> None:
        if (self.end is None) != (self.every_minutes == 0):
            raise ValueError(f"{self.job}: a repeating slot needs both end and every_minutes")
        if self.end is not None and self.end <= self.start:
            raise ValueError(f"{self.job}: end {self.end} is not after start {self.start}")

    def times(self) -> list[time]:
        if self.end is None:
            return [self.start]
        out, at = [], _minutes(self.start)
        while at < _minutes(self.end):
            out.append(_time(at))
            at += self.every_minutes
        return [*out, self.end]

    @property
    def last(self) -> time:
        return self.end or self.start

    def applies_to(self, early_close: bool) -> bool:
        return self.close is Close.ANY or (self.close is Close.EARLY) == early_close


# The evening fallbacks fire this often until their deadline, so a predecessor that finishes
# late still starts its successor within this many minutes.
FALLBACK_EVERY_MINUTES = 10

_DAY_SLOTS: tuple[Slot, ...] = (
    Slot("watchdog", time(7, 0), time(22, 0), 15, heartbeat=Heartbeat.NONE),
    Slot("execute_open", time(9, 20)),
    Slot("execute_open", time(9, 31)),
    Slot("execute_entries", time(9, 36)),
    Slot("stop_guard", time(9, 35), time(16, 0), 5, heartbeat=Heartbeat.NONE),
    Slot("eod_review", time(12, 40), close=Close.EARLY),
    Slot("eod_review", time(15, 40), close=Close.REGULAR),
)


def skeleton_slots(chain_wait_minutes: int) -> tuple[Slot, ...]:
    """The skeleton's weekday schedule. Each chained job after `post_market` fires from its
    fallback start through its deadline (fallback + `jobs.chain_wait_minutes`), where a run
    without a successful predecessor fails closed."""
    head, *rest = EVENING_CHAIN
    chain = [Slot(head.job, head.fallback_start)] + [
        Slot(
            link.job,
            link.fallback_start,
            _time(_minutes(link.fallback_start) + chain_wait_minutes),
            FALLBACK_EVERY_MINUTES,
            heartbeat=Heartbeat.BY_END,
        )
        for link in rest
    ]
    return (*_DAY_SLOTS, *chain)


@dataclass(frozen=True)
class Expected:
    """A heartbeat the `watchdog` expects: a run of `job` for the session, started at or
    after `after` (None: any time), by `due`."""

    job: str
    after: datetime | None
    due: datetime

    @property
    def label(self) -> str:
        if self.after is None:
            return f"{self.job} by {self.due.astimezone(NY):%H:%M}"
        return f"{self.job} {self.after.astimezone(NY):%H:%M}"


def expected_heartbeats(
    slots: tuple[Slot, ...], day: date, *, early_close: bool, grace_minutes: int
) -> list[Expected]:
    """Every heartbeat a session day should produce, in time order."""
    grace = timedelta(minutes=grace_minutes)
    out = []
    for slot in slots:
        if slot.heartbeat is Heartbeat.NONE or not slot.applies_to(early_close):
            continue
        if slot.heartbeat is Heartbeat.AT_START:
            start = datetime.combine(day, slot.start, tzinfo=NY)
            out.append(Expected(slot.job, start, start + grace))
        else:
            out.append(
                Expected(slot.job, None, datetime.combine(day, slot.last, tzinfo=NY) + grace)
            )
    return sorted(out, key=lambda e: e.due)


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _time(minutes: int) -> time:
    return time(minutes // 60, minutes % 60)

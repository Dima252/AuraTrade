"""Base detection and IBD base counting (archived README §5.3, task 2.2).

One forward pass over split-adjusted daily bars. Positions are session indexes into ``bars``; the
caller maps them to dates.

**Candidates.** A candidate starts at the first bar, or at the session that ended the previous
candidate, and runs until a close above its pivot. Inside it, a zigzag with a fixed
``swing_reversal`` marks the **contractions** (a swing high -> the lowest low before price
reverses up by ``swing_reversal``).

- **Pivot** at session ``t`` (from sessions before ``t`` only): the high of the final contraction
  when it sits in the upper part of the candidate's range (``pivot_min_position``; IBD's handle
  in the upper half), else the candidate's highest high. Without contractions, the highest high.
- **Breakout:** the first close above the pivot ends the candidate. It is a **base** only if it
  lasted at least ``min_sessions``. A shorter consolidation is ignored: it neither counts nor
  resets, and the next advance is measured from the last real base's pivot. Its lows still count
  as interim lows for the undercut check.
- **Failure:** a candidate deeper than ``max_depth`` (from its highest high to its lowest low) is
  not a base. It ends there and resets the count: the next base is stage 1.

**Counting** (IBD, with the user's rulings of 2026-10-10):

- A base is a new stage only after an advance of at least ``new_stage_advance`` from the prior
  base's pivot to the highest high before this base's decline. A smaller advance makes it a
  base-on-base, which keeps the prior stage.
- The count resets to 1 when any intraday low after the prior base (between the two bases, or
  inside this one) undercuts the prior base's low, or after a failed candidate.
- A count is **reliable** only when the sequence started at the listing (the history begins at
  the IPO) or at a reset seen in the data. Otherwise earlier, unseen bases may exist; the count is
  then a lower bound, and the caller treats it conservatively.
"""

from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Self

from auratrade.indicators.bars import OHLCV
from auratrade.indicators.series import check_window


class BaseStatus(StrEnum):
    FORMING = "FORMING"
    BROKEN_OUT = "BROKEN_OUT"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class BaseRules:
    """Thresholds from ``config/risk.yaml`` ``bases:`` (``BasesConfig`` has the same fields)."""

    min_sessions: int
    max_depth: float
    swing_reversal: float
    pivot_min_position: float
    new_stage_advance: float

    def __post_init__(self) -> None:
        check_window(self.min_sessions, "min_sessions")
        for name in ("max_depth", "swing_reversal", "pivot_min_position"):
            if not 0 < getattr(self, name) < 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.new_stage_advance <= 0:
            raise ValueError("new_stage_advance must be > 0")


@dataclass(frozen=True, slots=True)
class Contraction:
    high_session: int
    high: float
    low_session: int
    low: float

    @property
    def depth(self) -> float:
        return 1 - self.low / self.high


@dataclass(frozen=True, slots=True)
class PriceBase:
    start: int
    end: int | None
    """The session that ended it: the breakout (``BROKEN_OUT``) or the session it became too deep
    (``FAILED``). ``None`` while ``FORMING``."""
    status: BaseStatus
    sessions: int
    """Sessions inside the base, the ending session excluded."""
    high: float
    """The highest intraday high inside the base."""
    low: float
    low_session: int
    pivot: float
    """The pivot in force when it ended (the current one while forming)."""
    contractions: tuple[Contraction, ...]
    stage: int | None
    """``None`` for a failed candidate, which is not a base."""
    count_reliable: bool
    advance: float | None
    """From the prior base's pivot to the high before this base's decline. ``None`` for a first
    base or a reset."""
    reset: bool
    """This base restarted the count at stage 1, after an undercut or a failed candidate."""

    @property
    def depth(self) -> float:
        return 1 - self.low / self.high


@dataclass(frozen=True, slots=True)
class BaseCountState:
    """What counting needs from history before ``bars[0]``, carried from the stored bases.

    ``last_*`` describe the last real base that broke out. The bars passed with a state start
    at the session the state is as of: for ``after(base)``, the breakout session, which opens
    the next candidate. A run carried forward this way matches one pass over the whole history.
    """

    last_stage: int | None = None
    last_low: float | None = None
    last_pivot: float | None = None
    count_reliable: bool = False
    low_since_last: float | None = None
    """Lowest intraday low since the last base ended, outside the current candidate."""
    high_since_last: float | None = None
    """Highest intraday high since the last base ended, outside the current candidate."""
    reset_pending: bool = False
    """A failed candidate since the last base: the next base is a reset stage 1."""

    @classmethod
    def at_listing(cls) -> Self:
        """For a history that starts at the IPO: the first base is a reliable stage 1."""
        return cls(count_reliable=True)

    @classmethod
    def after(cls, base: PriceBase) -> Self:
        """The state at the session ``base`` broke out; pass bars from that session on."""
        if base.status is not BaseStatus.BROKEN_OUT or base.stage is None:
            raise ValueError("state can only be carried from a base that broke out")
        return cls(
            last_stage=base.stage,
            last_low=base.low,
            last_pivot=base.pivot,
            count_reliable=base.count_reliable,
        )


@dataclass(frozen=True, slots=True)
class BaseScan:
    bases: tuple[PriceBase, ...]
    """Oldest first: completed bases, failed candidates that lasted ``min_sessions``, and the
    forming base once it is long enough."""
    forming: PriceBase | None
    """The open candidate at the last bar, whatever its length, with the stage it would have."""
    state: BaseCountState
    """The counting state at the last bar, outside the open candidate."""


def _lower(a: float | None, b: float) -> float:
    return b if a is None else min(a, b)


def _higher(a: float | None, b: float) -> float:
    return b if a is None else max(a, b)


class _Candidate:
    def __init__(self, start: int, bar: OHLCV, reversal: float) -> None:
        self.start = start
        self.reversal = reversal
        self.high = bar.high
        self.low = bar.low
        self.low_session = start
        self.contractions: list[Contraction] = []
        # Rising: tracking the next swing high. Falling: extending the open contraction's low.
        self.rising = True
        self.swing_high = bar.high
        self.swing_high_session = start

    def pivot(self, min_position: float) -> float:
        if self.contractions:
            final = self.contractions[-1].high
            if final >= self.low + min_position * (self.high - self.low):
                return final
        return self.high

    @property
    def left_high(self) -> float:
        """The high the base's decline started from."""
        return self.contractions[0].high if self.contractions else self.high

    @property
    def depth(self) -> float:
        return 1 - self.low / self.high

    def add(self, t: int, bar: OHLCV) -> None:
        self.high = max(self.high, bar.high)
        if bar.low < self.low:
            self.low, self.low_session = bar.low, t
        # A bar either extends the current swing or reverses it, never both: the order of the
        # high and the low within one bar is unknown.
        if self.rising:
            if bar.high > self.swing_high:
                self.swing_high, self.swing_high_session = bar.high, t
            elif bar.low <= self.swing_high * (1 - self.reversal):
                self.rising = False
                self.contractions.append(
                    Contraction(self.swing_high_session, self.swing_high, t, bar.low)
                )
            return
        last = self.contractions[-1]
        if bar.low < last.low:
            self.contractions[-1] = replace(last, low_session=t, low=bar.low)
        elif bar.high >= last.low * (1 + self.reversal):
            self.rising = True
            self.swing_high, self.swing_high_session = bar.high, t


def _count(
    cand: _Candidate, state: BaseCountState, rules: BaseRules
) -> tuple[int, bool, float | None, bool]:
    """Stage, reliability, advance and reset flag for ``cand`` as a base."""
    if state.reset_pending:
        return 1, True, None, True
    if state.last_stage is None or state.last_low is None or state.last_pivot is None:
        return 1, state.count_reliable, None, False
    if _lower(state.low_since_last, cand.low) < state.last_low:
        return 1, True, None, True
    advance = _higher(state.high_since_last, cand.left_high) / state.last_pivot - 1
    stage = state.last_stage + 1 if advance >= rules.new_stage_advance else state.last_stage
    return stage, state.count_reliable, advance, False


def _as_base(
    cand: _Candidate,
    *,
    end: int | None,
    sessions: int,
    status: BaseStatus,
    pivot: float,
    state: BaseCountState,
    rules: BaseRules,
) -> PriceBase:
    if status is BaseStatus.FAILED:
        stage, reliable, advance, reset = None, state.count_reliable, None, False
    else:
        stage, reliable, advance, reset = _count(cand, state, rules)
    return PriceBase(
        start=cand.start,
        end=end,
        status=status,
        sessions=sessions,
        high=cand.high,
        low=cand.low,
        low_session=cand.low_session,
        pivot=pivot,
        contractions=tuple(cand.contractions),
        stage=stage,
        count_reliable=reliable,
        advance=advance,
        reset=reset,
    )


def _walk(
    bars: Sequence[OHLCV], rules: BaseRules, state: BaseCountState, found: list[PriceBase]
) -> Iterator[tuple[_Candidate, BaseCountState]]:
    """The one forward pass: after each bar, the open candidate and the counting state outside
    it. Completed bases and long-enough failed candidates go to ``found``."""
    cand = _Candidate(0, bars[0], rules.swing_reversal)
    yield cand, state
    for t in range(1, len(bars)):
        bar = bars[t]
        pivot = cand.pivot(rules.pivot_min_position)
        if bar.close > pivot:
            sessions = t - cand.start
            if sessions >= rules.min_sessions:
                base = _as_base(
                    cand,
                    end=t,
                    sessions=sessions,
                    status=BaseStatus.BROKEN_OUT,
                    pivot=pivot,
                    state=state,
                    rules=rules,
                )
                found.append(base)
                state = BaseCountState.after(base)
            else:
                state = replace(
                    state,
                    low_since_last=_lower(state.low_since_last, cand.low),
                    high_since_last=_higher(state.high_since_last, cand.high),
                )
            cand = _Candidate(t, bar, rules.swing_reversal)
            yield cand, state
            continue

        cand.add(t, bar)
        if cand.depth > rules.max_depth:
            sessions = t - cand.start
            if sessions >= rules.min_sessions:
                found.append(
                    _as_base(
                        cand,
                        end=t,
                        sessions=sessions,
                        status=BaseStatus.FAILED,
                        pivot=pivot,
                        state=state,
                        rules=rules,
                    )
                )
            state = replace(
                state,
                low_since_last=_lower(state.low_since_last, cand.low),
                high_since_last=_higher(state.high_since_last, cand.high),
                count_reliable=True,  # the count restarts here, so it no longer depends on history
                reset_pending=True,
            )
            cand = _Candidate(t, bar, rules.swing_reversal)
        yield cand, state


def detect_bases(
    bars: Sequence[OHLCV], rules: BaseRules, state: BaseCountState | None = None
) -> BaseScan:
    """Detect and count bases over ``bars`` (split-adjusted, oldest first).

    ``state`` carries the count from earlier history. Without it the first base is stage 1 with
    an unreliable count; pass ``BaseCountState.at_listing()`` when the bars start at the IPO.
    A completed base depends only on bars up to its end, so any prefix of the history yields the
    same completed bases.
    """
    state = state or BaseCountState()
    if not bars:
        return BaseScan(bases=(), forming=None, state=state)

    found: list[PriceBase] = []
    ((cand, state),) = deque(_walk(bars, rules, state, found), maxlen=1)
    forming = _as_base(
        cand,
        end=None,
        sessions=len(bars) - cand.start,
        status=BaseStatus.FORMING,
        pivot=cand.pivot(rules.pivot_min_position),
        state=state,
        rules=rules,
    )
    if forming.sessions >= rules.min_sessions:
        found.append(forming)
    return BaseScan(bases=tuple(found), forming=forming, state=state)


def forming_stages(
    bars: Sequence[OHLCV], rules: BaseRules, state: BaseCountState | None = None
) -> list[tuple[int, bool]]:
    """The forming base's stage and count reliability at each bar, as ``detect_bases`` over
    ``bars[: t + 1]`` gives them in ``forming``, in one pass: the base count a setup on session
    ``t`` reads (point in time)."""
    if not bars:
        return []
    out = []
    for cand, at in _walk(bars, rules, state or BaseCountState(), []):
        stage, reliable, _, _ = _count(cand, at, rules)
        out.append((stage, reliable))
    return out

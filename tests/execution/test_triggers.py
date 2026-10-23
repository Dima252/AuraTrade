"""Entry triggers, limits and the 09:36 order type (README §7.1)."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from auratrade.config import TriggerKind, load_risk_config
from auratrade.execution.triggers import (
    EntryLevels,
    OpeningRange,
    OrderKind,
    Skip,
    SkipReason,
    chased_too_far,
    entry_levels,
    limit_price,
    low_of_day_stop,
    opening_range,
    order_decision,
    session_low,
    trigger_level,
)

RISK = load_risk_config()
ENTRIES = RISK.entries
CHASE = RISK.guardrails.max_chase_above_pivot
OPEN = datetime(2026, 10, 22, 13, 30, tzinfo=UTC)  # 09:30 ET


@dataclass(frozen=True)
class Minute:
    start: datetime
    high: float
    low: float


def minutes(*ranges: tuple[float, float]) -> list[Minute]:
    """One bar a minute from 09:30, (high, low) each."""
    return [Minute(OPEN + timedelta(minutes=i), h, lo) for i, (h, lo) in enumerate(ranges)]


def levels(kind: TriggerKind, opening: OpeningRange | None, **kw: float) -> EntryLevels | Skip:
    args = {"level": 100.0, "code_pivot": 100.0, "adr": 0.06} | kw
    return entry_levels(kind, opening=opening, max_chase=CHASE, config=ENTRIES, **args)


# --- Opening range ------------------------------------------------------------------------


def test_opening_range_is_the_first_five_minutes() -> None:
    bars = minutes((99, 98), (101, 99), (100.5, 99.5), (100, 99), (100.2, 99.8), (110, 90))
    assert opening_range(bars, open_at=OPEN, minutes=5) == OpeningRange(high=101, low=98)


def test_no_bars_in_the_range_is_no_opening_range() -> None:
    """Thin IEX: no print in 09:30-09:34 (the caller falls back to delayed SIP, #109)."""
    late = [Minute(OPEN + timedelta(minutes=6), 100, 99)]
    assert opening_range(late, open_at=OPEN, minutes=5) is None


def test_session_low_up_to_the_order_time() -> None:
    bars = minutes((101, 97), (102, 98), (103, 99), (104, 99), (105, 99), (106, 96), (90, 80))
    assert session_low(bars, open_at=OPEN, until=OPEN + timedelta(minutes=6)) == 96
    assert session_low([], open_at=OPEN, until=OPEN) is None


# --- Triggers ------------------------------------------------------------------------------


def test_pivot_trigger_below_the_range_stays_at_the_pivot() -> None:
    assert trigger_level(TriggerKind.PIVOT, 100.0, OpeningRange(99.5, 98)) == 100.0
    assert trigger_level(TriggerKind.PIVOT, 100.0, None) == 100.0


def test_opened_below_the_pivot_and_broke_it_before_0936() -> None:
    """The opening range traded above the pivot: the opening-range high is the trigger, so the
    order isn't below the market (README §7.1 "Pivot crossed before 09:36")."""
    bars = minutes((99.5, 98.5), (100.4, 99.4), (101.2, 100.1), (101.0, 100.5), (100.9, 100.4))
    opening = opening_range(bars, open_at=OPEN, minutes=ENTRIES.opening_range_minutes)
    result = levels(TriggerKind.PIVOT, opening)
    assert result == EntryLevels(trigger=101.2, limit=103.22)
    # 09:36 at 101.0: still below the trigger, so a buy-stop-limit.
    assert isinstance(result, EntryLevels)
    assert order_decision(101.0, result) is OrderKind.STOP_LIMIT


def test_breaks_its_opening_range_high_at_0935() -> None:
    """Past the trigger but within the limit at 09:36: a limit buy at the limit."""
    bars = minutes((100.5, 99.8), (100.8, 100.1), (101.0, 100.3), (100.9, 100.4), (100.95, 100.6))
    opening = opening_range(bars, open_at=OPEN, minutes=ENTRIES.opening_range_minutes)
    result = levels(TriggerKind.PIVOT, opening)
    assert isinstance(result, EntryLevels)
    assert result.trigger == 101.0
    assert order_decision(101.6, result) is OrderKind.LIMIT
    above = order_decision(result.limit + 0.01, result)
    assert isinstance(above, Skip)
    assert above.reason is SkipReason.ABOVE_LIMIT


def test_opening_range_above_the_max_chase_is_skipped() -> None:
    result = levels(TriggerKind.PIVOT, OpeningRange(105.5, 101))
    assert isinstance(result, Skip)
    assert result.reason is SkipReason.MAX_CHASE
    assert not chased_too_far(105.0, 100.0, CHASE)  # exactly 5% is allowed
    assert chased_too_far(105.01, 100.0, CHASE)


def test_max_chase_is_from_the_code_pivot() -> None:
    """A pivot nudged up 3% doesn't extend the chase: 104 + range at 105.5 is still skipped."""
    result = levels(TriggerKind.PIVOT, OpeningRange(105.5, 103), level=103.0)
    assert isinstance(result, Skip)
    assert result.reason is SkipReason.MAX_CHASE


def test_momentum_trigger_is_the_higher_of_consolidation_and_range() -> None:
    kind = TriggerKind.CONSOLIDATION_OR_RANGE_HIGH
    assert trigger_level(kind, 100.0, OpeningRange(98, 96)) == 100.0
    assert trigger_level(kind, 100.0, OpeningRange(102, 99)) == 102.0
    assert trigger_level(kind, 100.0, None) is None


def test_ep_trigger_is_its_opening_range_high_and_the_cap_never_binds() -> None:
    kind = TriggerKind.OPENING_RANGE_HIGH
    assert trigger_level(kind, 50.0, None) is None
    result = levels(kind, OpeningRange(120.0, 112.0), level=110.0, code_pivot=110.0)
    # The gap-day range is far above the proxy pivot; the limit is ORH x 1.02 regardless.
    assert result == EntryLevels(trigger=120.0, limit=122.4)


def test_no_opening_range_skips_range_entries_only() -> None:
    result = levels(TriggerKind.OPENING_RANGE_HIGH, None)
    assert isinstance(result, Skip)
    assert result.reason is SkipReason.NO_OPENING_RANGE
    assert isinstance(levels(TriggerKind.PIVOT, None), EntryLevels)


# --- Limits --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("trigger", "adr", "code_pivot", "limit"),
    [
        (100.0, 0.06, 100.0, 102.0),  # 2% cap
        (100.0, 0.02, 100.0, 101.0),  # ½ x ADR
        (104.0, 0.06, 100.0, 105.0),  # code pivot x 1.05 binds
        (33.333, 0.06, 33.333, 33.99),  # rounded down to a tick
    ],
)
def test_limit_price(trigger: float, adr: float, code_pivot: float, limit: float) -> None:
    assert (
        limit_price(trigger, adr=adr, code_pivot=code_pivot, max_chase=CHASE, config=ENTRIES)
        == limit
    )


def test_trigger_rounds_up_and_limit_below_it_is_skipped() -> None:
    result = levels(TriggerKind.PIVOT, None, level=100.001, adr=0.000001)
    assert isinstance(result, Skip)
    assert result.reason is SkipReason.LIMIT_BELOW_TRIGGER


@pytest.mark.parametrize("bad", [float("nan"), 0.0, -1.0, float("inf")])
def test_invalid_levels_are_skipped(bad: float) -> None:
    result = levels(TriggerKind.PIVOT, None, level=bad)
    assert isinstance(result, Skip)
    assert result.reason is SkipReason.INVALID


# --- Order type and stops -----------------------------------------------------------------


@pytest.mark.parametrize("price", [None, float("nan"), 0.0])
def test_no_price_skips(price: float | None) -> None:
    result = order_decision(price, EntryLevels(100.0, 102.0))
    assert isinstance(result, Skip)
    assert result.reason is SkipReason.NO_PRICE


def test_order_never_buys_above_the_limit() -> None:
    lv = EntryLevels(100.0, 102.0)
    assert order_decision(99.0, lv) is OrderKind.STOP_LIMIT
    assert order_decision(100.0, lv) is OrderKind.STOP_LIMIT
    assert order_decision(102.0, lv) is OrderKind.LIMIT
    assert isinstance(order_decision(102.01, lv), Skip)


def test_low_of_day_stop_never_below_the_invalidation_level() -> None:
    assert low_of_day_stop(96.123, 95.0) == 96.13
    assert low_of_day_stop(94.0, 95.0) == 95.0

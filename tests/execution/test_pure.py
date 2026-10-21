"""Pure pieces of the order layer: the decision price, ids, and the skeleton's levels and sizing."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from auratrade.config import load_risk_config
from auratrade.data.alpaca_data import Quote, Trade
from auratrade.execution import ids
from auratrade.execution.lock import direct_conninfo
from auratrade.execution.prices import (
    decision_price,
    marketable_sell_limit,
    round_down,
    round_up,
    tick,
)
from auratrade.execution.skeleton import entry_decision, entry_levels, passes_scan, size
from auratrade.indicators.bars import Bar

RISK = load_risk_config()
CFG = RISK.execution.decision_price
NOW = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)


def trade(price: float, age_s: float) -> Trade:
    return Trade("X", price, 100, NOW - timedelta(seconds=age_s))


def quote(bid: float, ask: float, age_s: float) -> Quote:
    return Quote("X", bid, ask, 1, 1, NOW - timedelta(seconds=age_s))


@pytest.mark.parametrize(
    ("t", "q", "expected"),
    [
        (trade(10.0, 119), quote(9, 11, 0), 10.0),  # a fresh trade wins
        (trade(10.0, 121), quote(9.99, 10.01, 30), 10.0),  # stale trade: the midpoint
        (None, quote(9.99, 10.01, 61), None),  # stale quote
        (None, quote(9.0, 11.0, 0), None),  # spread 20% > 2%
        (None, quote(0.0, 10.0, 0), None),  # no bid
        (None, quote(10.1, 10.0, 0), None),  # crossed
        (None, None, None),
    ],
)
def test_decision_price(t: Trade | None, q: Quote | None, expected: float | None) -> None:
    assert decision_price(t, q, NOW, CFG) == expected


def test_ticks_and_rounding() -> None:
    assert (tick(1.0), tick(0.99)) == (0.01, 0.0001)
    assert (round_down(12.349), round_up(12.341)) == (12.34, 12.35)
    assert (round_down(12.34), round_up(12.34)) == (12.34, 12.34)
    assert round_up(0.12341) == 0.1235
    assert marketable_sell_limit(101.0, 0.005) == 100.49


def test_client_order_ids_round_trip() -> None:
    day = date(2026, 10, 22)
    assert ids.entry_id(day, "BRK.B", tranche=2, attempt=3) == "at-20261022-BRK.B-e2-3"
    entry = ids.parse(ids.entry_id(day, "NVDA"))
    assert entry == ids.ParsedId(day, "NVDA", ids.Purpose.ENTRY, "1")
    stop = ids.parse(ids.stop_id(day, "NVDA"))
    assert stop is not None
    assert (stop.purpose, stop.detail) == (ids.Purpose.STOP, "")
    sale = ids.parse(ids.exit_id(day, "NVDA", "HARD_STOP"))
    assert sale is not None
    assert (sale.purpose, sale.detail) == (ids.Purpose.EXIT, "HARD_STOP")
    assert ids.stop_id(day, "NVDA") != ids.stop_id(day, "NVDA")


@pytest.mark.parametrize(
    "value",
    [
        "1a6286c9-9a8a-4d12-8eef-84adff10fc7d",  # a leg's id, set by Alpaca
        "spk-session-5fc8675b",
        "at-2026xx22-NVDA-e1-1",
        "at-20261022-NVDA-q1-1",
        "at-20261022-NVDA-e-1",
    ],
)
def test_other_ids_are_not_ours(value: str) -> None:
    assert ids.parse(value) is None


def test_direct_conninfo_swaps_the_neon_pooler_for_the_direct_host() -> None:
    pooled = "postgresql+psycopg://u:p@ep-cool-1-pooler.us-east-2.aws.neon.tech/db?sslmode=require"
    direct = direct_conninfo(pooled)
    assert direct.startswith("postgresql://u:p@ep-cool-1.us-east-2.aws.neon.tech/db")
    assert direct_conninfo("postgresql+psycopg://u:p@localhost:5434/db") == (
        "postgresql://u:p@localhost:5434/db"
    )


# --- the skeleton -----------------------------------------------------------------------------


def bars(
    n: int, *, high: float = 100.0, low: float = 95.0, last_low: float | None = None
) -> list[Bar]:
    out = [Bar(97.0, high, low, 98.0, 1e6) for _ in range(n)]
    if last_low is not None:
        out[-1] = Bar(97.0, high, last_low, 98.0, 1e6)
    return out


def test_entry_levels() -> None:
    sk = RISK.skeleton
    levels = entry_levels(bars(30), sk, max_chase=0.05)
    assert levels is not None
    # ADR = 100/95 - 1 = 5.3%: the limit is min(2%, 2.6%) above the trigger.
    assert (levels.pivot, levels.trigger, levels.limit) == (100.0, 100.0, 102.0)
    assert levels.stop == 95.0  # the 10-session low, within 8% of the limit
    deep = entry_levels(bars(30, last_low=80.0), sk, max_chase=0.05)
    assert deep is not None
    assert deep.stop == round(102.0 * 0.92, 2)  # capped at 8% below the limit
    assert entry_levels(bars(10), sk, max_chase=0.05) is None


def test_entry_levels_reject_inverted_levels() -> None:
    flat = [Bar(100.0, 100.0, 100.0, 100.0, 1e6) for _ in range(30)]
    assert entry_levels(flat, RISK.skeleton, max_chase=0.05) is None


def test_the_scan_needs_rules_1_to_5_and_8() -> None:
    rising = [Bar(c, c * 1.01, c * 0.99, c, 1e6) for c in (50 + i * 0.2 for i in range(300))]
    assert passes_scan(rising, 90.0, RISK.skeleton, RISK.trend_template)
    assert not passes_scan(rising, 50.0, RISK.skeleton, RISK.trend_template)  # rule 8
    assert not passes_scan(rising[:100], 90.0, RISK.skeleton, RISK.trend_template)
    assert not passes_scan([], 90.0, RISK.skeleton, RISK.trend_template)


@pytest.mark.parametrize(
    ("kwargs", "shares"),
    [
        ({}, 7),  # $5,000 x 1% / $7 = 7.1
        ({"room": 300.0}, 2),  # exposure left
        ({"max_position_fraction": 0.05}, 2),  # $250 / $102
        ({"stop": 102.0}, 0),
        ({"equity": 0.0}, 0),
        ({"room": -10.0}, 0),
    ],
)
def test_size(kwargs: dict[str, float], shares: int) -> None:
    args = {
        "equity": 5000.0,
        "risk_fraction": 0.01,
        "limit": 102.0,
        "stop": 95.0,
        "max_position_fraction": 0.25,
        "room": 5000.0,
    } | kwargs
    assert size(**args) == shares


def test_entry_decision() -> None:
    assert entry_decision(99.0, trigger=100.0, limit=102.0).trigger == 100.0
    past = entry_decision(101.0, trigger=100.0, limit=102.0)
    assert (past.trigger, past.skip) == (None, None)
    assert entry_decision(102.5, trigger=100.0, limit=102.0).skip is not None
    assert entry_decision(None, trigger=100.0, limit=102.0).skip == "no decision price"

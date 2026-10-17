from __future__ import annotations

import pytest

from auratrade.indicators.universe import universe_flags

FILTERS = {
    "min_price": 10.0,
    "min_dollar_volume": 1_000.0,
    "dollar_volume_sessions": 2,
    "min_history_sessions": 3,
}


def test_needs_history_price_and_dollar_volume_as_of_each_bar() -> None:
    closes = [5.0, 5.0, 5.0, 5.0, 5.0, 5.0]
    # Raw closes as traded: a 2-for-1 split after bar 3 halves the price.
    raw = [20.0, 20.0, 20.0, 20.0, 10.0, 9.0]
    volumes = [400.0, 400.0, 400.0, 100.0, 100.0, 300.0]
    flags = universe_flags(closes, raw, volumes, **FILTERS)
    # Bar 2: third bar, $2,000 average; bar 3: $1,250; bar 4: $500 (too thin); bar 5: $9 raw.
    assert flags == [False, False, True, True, False, False]


def test_lengths_must_match() -> None:
    with pytest.raises(ValueError, match="lengths differ"):
        universe_flags([1.0], [1.0, 2.0], [1.0], **FILTERS)

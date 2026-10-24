"""Stored entries round-trip through JSON (journal.records)."""

from auratrade.journal.records import StoredEntry
from tests.guardrails.test_orders import approved_vcp


def stored(tested_ema: int | None) -> StoredEntry:
    return StoredEntry(
        approved_vcp(),
        code_pivot=100.0,
        atr=2.5,
        adr=0.04,
        dollar_volume=5e7,
        equity=5000.0,
        record=None,
        tested_ema=tested_ema,
    )


def test_round_trip() -> None:
    s = stored(21)
    assert StoredEntry.from_json(s.to_json()) == s


def test_rows_stored_before_the_rename_keep_their_window() -> None:
    data = stored(None).to_json()
    del data["tested_ema"]
    data["tested_ma"] = 10
    assert StoredEntry.from_json(data).tested_ema == 10

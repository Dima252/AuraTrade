"""Backfill stage: VIX and VIX3M closes from CBOE's history files, as the backtests use them
(README §5.1): filtered to sessions, since the VIX file has rows on holidays. The credit spread
has only 3 years of history at FRED and no code rule reads it, so it isn't backfilled."""

from __future__ import annotations

from collections.abc import Collection
from datetime import date
from typing import Protocol

import pyarrow as pa

from auratrade.data.macro import CboeHistory, CboeIndex
from auratrade.history.store import put_table, table_key
from auratrade.ingest.macro import CBOE_HISTORY
from auratrade.storage import ObjectStore

MACRO = "macro"
MACRO_SCHEMA = pa.schema(
    [
        ("series", pa.string()),
        ("observation_date", pa.date32()),
        ("value", pa.float64()),
        ("source", pa.string()),
    ]
)


class HistorySource(Protocol):
    def history(self, index: CboeIndex) -> CboeHistory: ...


def macro_history(store: ObjectStore, cboe: HistorySource, sessions: Collection[date]) -> int:
    rows = [
        {"series": index.value, "observation_date": day, "value": value, "source": CBOE_HISTORY}
        for index in (CboeIndex.VIX, CboeIndex.VIX3M)
        for day, value in sorted(cboe.history(index).closes.items())
        if day in sessions
    ]
    put_table(store, table_key(MACRO), pa.Table.from_pylist(rows, MACRO_SCHEMA))
    return len(rows)

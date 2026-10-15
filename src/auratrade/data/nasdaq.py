"""The Nasdaq Trader symbol directory (free, no key): its ETF flag is the first step of the
universe classification, because Alpaca's asset list has no type flag (README §5.1, #111).

`nasdaqlisted.txt` covers Nasdaq listings and `otherlisted.txt` the rest (NYSE, NYSE
American, NYSE Arca, Cboe BZX, IEX). Both are pipe-delimited with a "File Creation Time"
footer. ETNs carry `ETF = N` (VXX), so the classifier still needs its name rule for them.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from dataclasses import dataclass
from time import sleep as time_sleep

import httpx

from auratrade.data.http import RateLimiter, VendorError, VendorHttp

BASE_URL = "https://www.nasdaqtrader.com/dynamic/SymDir"
FOOTER = "File Creation Time"
# otherlisted.txt's exchange codes.
EXCHANGES = {
    "A": "NYSE American",
    "N": "NYSE",
    "P": "NYSE Arca",
    "Z": "Cboe BZX",
    "V": "IEX",
}


@dataclass(frozen=True)
class Listing:
    symbol: str  # CQS / ACT style, as Alpaca writes it (BRK.B)
    name: str
    exchange: str
    is_etf: bool
    is_test_issue: bool


class NasdaqDirectoryClient:
    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time_sleep,
    ) -> None:
        self._http = VendorHttp(
            "nasdaq",
            limiter=RateLimiter(per_second=1, sleep=sleep),
            client=client,
            sleep=sleep,
        )

    def listings(self) -> list[Listing]:
        """Both files, test issues included (callers drop them)."""
        nasdaq = parse_directory(self._http.get_text(f"{BASE_URL}/nasdaqlisted.txt"), nasdaq=True)
        other = parse_directory(self._http.get_text(f"{BASE_URL}/otherlisted.txt"), nasdaq=False)
        return nasdaq + other


def parse_directory(text: str, *, nasdaq: bool) -> list[Listing]:
    lines = [line for line in text.splitlines() if line and not line.startswith(FOOTER)]
    rows = list(csv.DictReader(io.StringIO("\n".join(lines)), delimiter="|"))
    symbol_field = "Symbol" if nasdaq else "ACT Symbol"
    if rows and symbol_field not in rows[0]:
        raise VendorError("nasdaq", f"directory without a {symbol_field!r} column", retryable=False)
    return [
        Listing(
            row[symbol_field],
            row["Security Name"],
            "NASDAQ" if nasdaq else EXCHANGES.get(row["Exchange"], row["Exchange"]),
            row["ETF"] == "Y",
            row["Test Issue"] == "Y",
        )
        for row in rows
    ]

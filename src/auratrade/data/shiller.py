"""Robert Shiller's monthly S&P 500 data: prices and dividends from 1871 (README §5.1,
`docs/backtests/portfolio-preregistration.md` §4.2). Free, no key.

Read from the Open Knowledge "s-and-p-500" dataset, which republishes Shiller's `ie_data.xls`
as CSV (its values match the Yale file's; checked 2026-10-27, #157). `Dividend` is the trailing
12-month dividend in index points; Shiller's dividends lag the prices by a few months, and the
CSV writes the missing ones as 0.0, read here as unknown.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from time import sleep as time_sleep

import httpx

from auratrade.data.http import RateLimiter, VendorError, VendorHttp

SHILLER_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500/main/data/data.csv"
SHILLER_PER_SECOND = 1


@dataclass(frozen=True, slots=True)
class ShillerMonth:
    month: date
    """The first of the month."""
    price: float
    """The S&P Composite's monthly average close."""
    dividend: float | None
    """The trailing 12-month dividend in index points; None where not yet published."""


class ShillerClient:
    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time_sleep,
    ) -> None:
        self._http = VendorHttp(
            "shiller",
            limiter=RateLimiter(per_second=SHILLER_PER_SECOND, sleep=sleep),
            client=client,
            sleep=sleep,
        )

    def monthly(self) -> list[ShillerMonth]:
        return parse_shiller_csv(self._http.get_text(SHILLER_URL))


def parse_shiller_csv(text: str) -> list[ShillerMonth]:
    months: list[ShillerMonth] = []
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or not {"Date", "SP500", "Dividend"} <= set(reader.fieldnames):
        raise VendorError("shiller", "no Date/SP500/Dividend columns", retryable=False)
    for row in reader:
        month = date.fromisoformat(row["Date"])
        if month.day != 1 or (months and month <= months[-1].month):
            raise VendorError("shiller", f"{month} is not the next month", retryable=False)
        dividend = float(row["Dividend"])
        months.append(ShillerMonth(month, float(row["SP500"]), dividend if dividend > 0 else None))
    if not months:
        raise VendorError("shiller", "no rows", retryable=False)
    return months

"""Kenneth French's data library: the daily US market and T-bill returns from 1926 (README §5.1,
`docs/backtests/portfolio-preregistration.md` §4.1). Free, no key.

The "Fama/French 3 Factors [Daily]" file is a zipped CSV: a few lines of notes, a header
`,Mkt-RF,SMB,HML,RF`, one row per session (`YYYYMMDD` and percentages), a blank line and a
copyright line. The market's total return is Mkt-RF + RF (the CRSP value-weighted market).
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from time import sleep as time_sleep

import httpx

from auratrade.data.http import RateLimiter, VendorError, VendorHttp

FRENCH_DAILY_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "F-F_Research_Data_Factors_daily_CSV.zip"
)
FRENCH_PER_SECOND = 1
HEADER = ("", "Mkt-RF", "SMB", "HML", "RF")


@dataclass(frozen=True, slots=True)
class MarketDay:
    day: date
    market: float
    """The market's total return that session, as a fraction (Mkt-RF + RF)."""
    rf: float
    """The T-bill return that session, as a fraction."""


@dataclass(frozen=True, slots=True)
class FrenchDaily:
    note: str
    """The file's first line, naming the CRSP release it was built from."""
    days: list[MarketDay]
    """Oldest first."""


class FrenchClient:
    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time_sleep,
    ) -> None:
        self._http = VendorHttp(
            "french",
            limiter=RateLimiter(per_second=FRENCH_PER_SECOND, sleep=sleep),
            client=client,
            sleep=sleep,
        )

    def daily_factors(self) -> FrenchDaily:
        return parse_french_zip(self._http.get(FRENCH_DAILY_URL).content)


def parse_french_zip(body: bytes) -> FrenchDaily:
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
            if len(names) != 1:
                raise VendorError("french", f"expected one CSV, got {names}", retryable=False)
            text = archive.read(names[0]).decode("utf-8")
    except zipfile.BadZipFile:
        raise VendorError("french", "not a zip file", retryable=False) from None
    return parse_french_daily(text)


def parse_french_daily(text: str) -> FrenchDaily:
    """The rows between the header and the first blank line; percentages → fractions."""
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if _cells(line) == HEADER)
    except StopIteration:
        raise VendorError("french", "no Mkt-RF header", retryable=False) from None
    days: list[MarketDay] = []
    for row in csv.reader(lines[start + 1 :]):
        if not row or not row[0].strip():
            break
        stamp, mkt_rf, _smb, _hml, rf = (cell.strip() for cell in row)
        excess, riskless = float(mkt_rf) / 100, float(rf) / 100
        day = date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:]))
        if days and day <= days[-1].day:
            raise VendorError("french", f"{day} out of order", retryable=False)
        days.append(MarketDay(day, excess + riskless, riskless))
    if not days:
        raise VendorError("french", "no rows", retryable=False)
    return FrenchDaily(lines[0].strip(), days)


def _cells(line: str) -> tuple[str, ...]:
    return tuple(cell.strip() for cell in line.split(","))

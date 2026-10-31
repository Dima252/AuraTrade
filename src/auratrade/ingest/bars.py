"""Daily-bar rules for ingest (README §4, §5.1), pure.

- **Sanity:** positive prices, a high and low that contain the open and close, volume > 0.
  Zero-volume bars are dropped: Alpaca repeats the last close with volume 0 for months after a
  delisting (#25). A dropped bar is a missing bar, never forward-filled.
- **Outliers:** SIP daily bars carry bad ticks that pass the sanity check (NVDA's high of 195.95
  on 2024-06-10, true about 122). A high (low) beyond both the bar's open-close body and its
  neighbours' highs (lows) by more than the configured floor, or a multiple of the stock's
  average daily range if that is larger, is replaced by the body's edge; the vendor's value
  goes to `bar_corrections` (#25). The newest bar has only one neighbour; it is checked again
  the next evening, when session T-1 is re-fetched.
- **Splits:** each evening re-fetches the last stored bar. If its split-adjusted close moved,
  the vendor re-adjusted the history (a split or reverse split), and the whole window is
  re-fetched.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum

from auratrade.config import OutlierConfig


@dataclass(frozen=True, slots=True)
class IngestBar:
    """A split-adjusted daily bar plus the close as it traded that day."""

    session: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    raw_close: float


class BarField(StrEnum):
    HIGH = "HIGH"
    LOW = "LOW"


@dataclass(frozen=True, slots=True)
class Correction:
    session: date
    field: BarField
    original: float
    corrected: float


def sanity_problem(bar: IngestBar) -> str | None:
    """Why the bar can't be stored, or None."""
    if bar.volume <= 0:
        return "zero volume"
    if min(bar.open, bar.high, bar.low, bar.close, bar.raw_close) <= 0:
        return "non-positive price"
    if bar.high < max(bar.open, bar.close, bar.low) or bar.low > min(bar.open, bar.close):
        return "high/low do not contain open and close"
    return None


def combine(
    adjusted: Sequence[IngestBar], raw_closes: Mapping[date, float]
) -> tuple[list[IngestBar], list[date]]:
    """Attach the raw close to each split-adjusted bar. A session with no raw bar can't pass
    the point-in-time price filter, so it is dropped and returned."""
    out, missing = [], []
    for bar in adjusted:
        raw = raw_closes.get(bar.session)
        if raw is None:
            missing.append(bar.session)
        else:
            out.append(replace(bar, raw_close=raw))
    return out, missing


def average_range(bars: Sequence[IngestBar]) -> float | None:
    """The average daily range (high / low - 1) of `bars`, None for none."""
    if not bars:
        return None
    return sum(b.high / b.low - 1 for b in bars) / len(bars)


def find_outliers(bars: Sequence[IngestBar], cfg: OutlierConfig) -> list[Correction]:
    """Bad highs and lows in `bars` (one security, oldest first, consecutive sessions).

    "Far" scales with the stock's own volatility: beyond the body and the neighbours by more
    than the floors or `adr_multiple` times its average daily range over the bars before it,
    whichever is larger, so a volatile small cap's real intraday spike is kept."""
    found = []
    for i, bar in enumerate(bars):
        neighbours = [bars[j] for j in (i - 1, i + 1) if 0 <= j < len(bars)]
        if not neighbours:
            continue
        adr = average_range(bars[max(i - cfg.adr_sessions, 0) : i]) or 0.0
        body = max(cfg.min_beyond_body, cfg.adr_multiple * adr)
        beside = max(cfg.min_beyond_neighbours, cfg.adr_multiple * adr)
        top, bottom = max(bar.open, bar.close), min(bar.open, bar.close)
        if bar.high > top * (1 + body) and bar.high > max(n.high for n in neighbours) * (
            1 + beside
        ):
            found.append(Correction(bar.session, BarField.HIGH, bar.high, top))
        if bar.low < bottom * (1 - body) and bar.low < min(n.low for n in neighbours) * (
            1 - beside
        ):
            found.append(Correction(bar.session, BarField.LOW, bar.low, bottom))
    return found


def apply_corrections(
    bars: Sequence[IngestBar], corrections: Sequence[Correction]
) -> list[IngestBar]:
    out = list(bars)
    index = {b.session: i for i, b in enumerate(out)}
    for c in corrections:
        i = index[c.session]
        if c.field is BarField.HIGH:
            out[i] = replace(out[i], high=c.corrected)
        else:
            out[i] = replace(out[i], low=c.corrected)
    return out


def split_changed(stored_close: float, fetched_close: float, tolerance: float) -> bool:
    """Whether a re-fetched bar's split-adjusted close differs from the stored one by more
    than `tolerance`: the vendor re-adjusted the history."""
    return abs(fetched_close / stored_close - 1) > tolerance


def coverage(expected: set[int], received: set[int]) -> float:
    """The share of `expected` securities with a bar; 1.0 when nothing was expected."""
    if not expected:
        return 1.0
    return len(expected & received) / len(expected)

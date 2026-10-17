"""Which securities the history holds, and how their bars are keyed (pure; README §5.1, #111).

- **Discovery:** Alpaca's asset list (active and inactive) forgets most delistings before ~2018
  and many after (26 of 48 known delistings were missing, spike 0.3a), but the bars endpoint
  still serves them. So every 1-4 letter ticker is also tried over a few sessions every
  `enumeration_every_sessions`; a symbol found only this way has no name, and its type is
  assumed to be common stock.
- **Keys:** bars come back by ticker, and tickers get reused. A ticker's bars split into separate
  securities wherever it didn't trade for more than `max_gap_sessions` sessions. The newest piece
  takes the Alpaca asset id of the ticker's active asset if it still trades (else an inactive
  one), the piece before it an inactive asset id not yet used, and the rest a key of the symbol
  and their first session. Ticker ids differ per database, so the history never uses them.
"""

from __future__ import annotations

import itertools
import re
import string
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from auratrade.ingest.classify import SecurityType

# Alpaca's inactive list holds junk codes (`0029900E0`) that fail a whole multi-symbol request,
# and renames some delisted assets `<SYM>_DELISTED` (spike 0.3a).
VALID_SYMBOL = re.compile(r"[A-Z]{1,5}(\.[A-Z]{1,2})?")
DELISTED_SUFFIX = "_DELISTED"


class Source(StrEnum):
    ACTIVE = "ACTIVE"  # in Alpaca's active asset list
    INACTIVE = "INACTIVE"  # in its inactive list only
    # Tied to no asset: found only by the ticker sweep, or an older piece of a reused ticker.
    ENUMERATED = "ENUMERATED"


@dataclass(frozen=True, slots=True)
class KnownAsset:
    """An Alpaca asset, classified (`ingest.classify`)."""

    asset_id: str
    symbol: str  # cleaned (`clean_symbol`)
    name: str
    exchange: str
    active: bool
    security_type: SecurityType


@dataclass(frozen=True, slots=True)
class Security:
    key: str
    symbol: str
    alpaca_asset_id: str | None
    name: str | None
    exchange: str | None
    security_type: SecurityType
    # True when nothing names the security (found by the sweep only): assumed common stock.
    type_assumed: bool
    source: Source
    first_session: date
    last_session: date
    # Whether the security still trades: the newest piece of a ticker with an active asset.
    active: bool


def clean_symbol(symbol: str) -> str | None:
    """The tradable symbol of an asset-list entry, None for junk codes."""
    cleaned = symbol.removesuffix(DELISTED_SUFFIX)
    return cleaned if VALID_SYMBOL.fullmatch(cleaned) else None


def sweep_tickers(max_letters: int = 4) -> list[str]:
    """Every ticker of 1 to `max_letters` capital letters (475,254 for 4)."""
    letters = string.ascii_uppercase
    return [
        "".join(p) for n in range(1, max_letters + 1) for p in itertools.product(letters, repeat=n)
    ]


def sweep_windows(sessions: Sequence[date], every: int, length: int) -> list[tuple[date, date]]:
    """(first, last) session of each sweep: `length` sessions starting every `every`, the last
    one ending on the final session so the newest listings are seen too."""
    if not sessions:
        return []
    starts = list(range(0, len(sessions), every))
    windows = [(sessions[i], sessions[min(i + length, len(sessions)) - 1]) for i in starts]
    tail = (sessions[max(len(sessions) - length, 0)], sessions[-1])
    if windows[-1] != tail:
        windows.append(tail)
    return windows


def segments(
    days: Sequence[date], position: Mapping[date, int], max_gap: int
) -> list[tuple[int, int]]:
    """Index ranges [first, last] of `days` (one ticker's traded sessions, oldest first) split
    wherever more than `max_gap` sessions passed without a bar. `position` maps a session to its
    index in the calendar."""
    out: list[tuple[int, int]] = []
    first = 0
    for i in range(1, len(days)):
        if position[days[i]] - position[days[i - 1]] - 1 > max_gap:
            out.append((first, i - 1))
            first = i
    if days:
        out.append((first, len(days) - 1))
    return out


def securities_for(
    symbol: str,
    pieces: Sequence[tuple[date, date]],
    assets: Iterable[KnownAsset],
    *,
    trading_since: date,
) -> list[Security]:
    """One security per piece (first and last session, oldest first) of `symbol`'s bars. The
    active asset claims the newest piece only if it traded on or after `trading_since`: an old
    piece of a reused ticker is not today's security."""
    known = sorted(assets, key=lambda a: (a.active, a.asset_id))
    active = next((a for a in reversed(known) if a.active), None)
    if active is not None and pieces and pieces[-1][1] < trading_since:
        active = None
    inactive = [a for a in known if not a.active]
    claims: list[KnownAsset | None] = [None] * len(pieces)
    if pieces:
        claims[-1] = active or (inactive.pop() if inactive else None)
    if len(pieces) > 1 and inactive:
        claims[-2] = inactive.pop()
    out = []
    for i, ((first, last), asset) in enumerate(zip(pieces, claims, strict=True)):
        if asset is None:
            out.append(
                Security(
                    key=f"{symbol}:{first.isoformat()}",
                    symbol=symbol,
                    alpaca_asset_id=None,
                    name=None,
                    exchange=None,
                    security_type=SecurityType.COMMON,
                    type_assumed=True,
                    source=Source.ENUMERATED,
                    first_session=first,
                    last_session=last,
                    active=False,
                )
            )
            continue
        out.append(
            Security(
                key=asset.asset_id,
                symbol=symbol,
                alpaca_asset_id=asset.asset_id,
                name=asset.name,
                exchange=asset.exchange,
                security_type=asset.security_type,
                type_assumed=False,
                source=Source.ACTIVE if asset.active else Source.INACTIVE,
                first_session=first,
                last_session=last,
                active=asset.active and i == len(pieces) - 1,
            )
        )
    return out

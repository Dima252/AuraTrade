"""Backfill stage 1: the symbols whose bars the history fetches (`securities` explains why).

Alpaca's active and inactive asset lists, classified (the Nasdaq Trader directory names and
flags the active ones; inactive assets keep Alpaca's name), plus the ticker sweep. Kept: common
stocks and ADRs on the universe exchanges, the regime's index ETFs, and every swept symbol that
no asset list names. Each sweep window is saved in the work folder as it finishes, so an
interrupted run resumes.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import structlog

from auratrade.config import RiskConfig
from auratrade.data.alpaca_data import Adjustment, Asset, DailyBar
from auratrade.data.nasdaq import Listing
from auratrade.history.securities import KnownAsset, clean_symbol, sweep_tickers, sweep_windows
from auratrade.ingest.classify import SecurityType, classify
from auratrade.ingest.universe import LISTED_EXCHANGES

log = structlog.get_logger(__name__)


class AssetSource(Protocol):
    def assets(self, *, status: str | None = ...) -> list[Asset]: ...

    def daily_bars(
        self,
        symbols: Iterable[str],
        start: date,
        end: date,
        *,
        adjustment: Adjustment = Adjustment.SPLIT,
    ) -> dict[str, list[DailyBar]]: ...


class DirectorySource(Protocol):
    def listings(self) -> list[Listing]: ...


@dataclass(frozen=True)
class Discovery:
    assets: list[KnownAsset]
    swept: list[str]  # symbols with a traded bar in some sweep window
    symbols: list[str]  # what the bars stage fetches, sorted

    def to_json(self) -> dict[str, object]:
        return {
            "assets": [
                {
                    "asset_id": a.asset_id,
                    "symbol": a.symbol,
                    "name": a.name,
                    "exchange": a.exchange,
                    "active": a.active,
                    "security_type": a.security_type.value,
                }
                for a in self.assets
            ],
            "swept": self.swept,
            "symbols": self.symbols,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Discovery:
        assets = [
            KnownAsset(
                str(a["asset_id"]),
                str(a["symbol"]),
                str(a["name"]),
                str(a["exchange"]),
                bool(a["active"]),
                SecurityType(a["security_type"]),
            )
            for a in data["assets"]
        ]
        return cls(assets, [str(s) for s in data["swept"]], [str(s) for s in data["symbols"]])


def known_assets(
    active: Iterable[Asset], inactive: Iterable[Asset], listings: Iterable[Listing]
) -> list[KnownAsset]:
    """Listed assets with a usable symbol, classified."""
    directory = {x.symbol: x for x in listings if not x.is_test_issue}
    out = []
    for a, is_active in [*((a, True) for a in active), *((a, False) for a in inactive)]:
        symbol = clean_symbol(a.symbol)
        if symbol is None or a.exchange not in LISTED_EXCHANGES:
            continue
        listing = directory.get(symbol) if is_active else None
        name = listing.name if listing is not None else a.name
        kind = classify(symbol, name, is_etf=listing is not None and listing.is_etf)
        out.append(KnownAsset(a.id, symbol, name, a.exchange, is_active, kind))
    return out


def wanted_symbols(
    assets: Sequence[KnownAsset], swept: Iterable[str], risk: RiskConfig
) -> list[str]:
    u = risk.universe
    named = {a.symbol for a in assets}
    kept = {
        a.symbol
        for a in assets
        if a.security_type in u.security_types and a.exchange in u.exchanges
    }
    kept |= set(risk.regime.indexes)
    kept |= {s for s in swept if s not in named}
    return sorted(kept)


def sweep(source: AssetSource, window: tuple[date, date], tickers: Sequence[str]) -> list[str]:
    """Tickers with a bar of positive volume in the window (zero-volume bars are zombies)."""
    bars = source.daily_bars(tickers, window[0], window[1], adjustment=Adjustment.RAW)
    return sorted(s for s, series in bars.items() if any(b.volume > 0 for b in series))


def discover(
    source: AssetSource,
    directory: DirectorySource,
    sessions: Sequence[date],
    risk: RiskConfig,
    work: Path,
    *,
    tickers: Sequence[str] | None = None,
) -> Discovery:
    """Run (or resume) discovery; the result is saved as `work/discovery.json`. `sessions`:
    the calendar's sessions from the history start through the last one fetched."""
    done = work / "discovery.json"
    if done.exists():
        return Discovery.from_json(json.loads(done.read_text(encoding="utf-8")))
    cfg = risk.history
    assets = known_assets(
        source.assets(status="active"), source.assets(status="inactive"), directory.listings()
    )
    candidates = list(tickers) if tickers is not None else sweep_tickers()
    folder = work / "sweep"
    folder.mkdir(parents=True, exist_ok=True)
    swept: set[str] = set()
    windows = sweep_windows(
        sessions, cfg.enumeration_every_sessions, cfg.enumeration_window_sessions
    )
    for i, window in enumerate(windows):
        path = folder / f"{window[0].isoformat()}.json"
        if path.exists():
            found = json.loads(path.read_text(encoding="utf-8"))
        else:
            found = sweep(source, window, candidates)
            path.write_text(json.dumps(found), encoding="utf-8")
        log.info("backfill_sweep", window=[d.isoformat() for d in window], found=len(found), n=i)
        swept |= set(found)
    result = Discovery(assets, sorted(swept), wanted_symbols(assets, swept, risk))
    done.write_text(json.dumps(result.to_json()), encoding="utf-8")
    return result

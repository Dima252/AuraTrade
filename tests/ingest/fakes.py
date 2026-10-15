"""Fakes and builders for the ingest tests: an in-memory bar vendor, the recorded calendar in the
test database, a risk config with short windows, and ticker rows."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import Connection, Engine, insert

from auratrade.calendar.sessions import MarketCalendar, load_calendar
from auratrade.calendar.sync import sync_market_sessions
from auratrade.config import DEFAULT_RISK_CONFIG, RiskConfig, load_risk_config
from auratrade.data.alpaca_data import Adjustment, DailyBar
from auratrade.db.models import Ticker
from tests.calendar_fixture import alpaca_times

RISK = load_risk_config(DEFAULT_RISK_CONFIG)
NOW = datetime(2026, 10, 15, 21, 30, tzinfo=UTC)


def small_risk(**ingest: Any) -> RiskConfig:
    """Short windows: 20 sessions of bars, 5-session dollar volume, 1 to 10 dollar filters."""
    universe = RISK.universe.model_copy(
        update={"dollar_volume_sessions": 5, "min_price": 10.0, "min_dollar_volume": 100_000.0}
    )
    cfg = RISK.ingest.model_copy(update={"bar_history_sessions": 20, **ingest})
    return RISK.model_copy(update={"universe": universe, "ingest": cfg})


def sync_calendar(engine: Engine) -> MarketCalendar:
    times = alpaca_times()
    sync_market_sessions(engine, times, times, start=times[0].day, end=times[-1].day, now=NOW)
    with engine.connect() as conn:
        return load_calendar(conn)


def add_ticker(
    conn: Connection,
    symbol: str,
    security_type: str = "COMMON",
    exchange: str = "NASDAQ",
    **values: Any,
) -> int:
    return int(
        conn.execute(
            insert(Ticker)
            .values(symbol=symbol, security_type=security_type, exchange=exchange, **values)
            .returning(Ticker.id)
        ).scalar_one()
    )


@dataclass
class FakeBars:
    """Bars per symbol and session; `split` maps symbol → ratio applied to adjusted history
    before `ex_date` (as the vendor re-adjusts after a split)."""

    raw: dict[str, dict[date, DailyBar]] = field(default_factory=dict)
    splits: dict[str, tuple[date, float]] = field(default_factory=dict)
    hidden: set[tuple[str, date]] = field(default_factory=set)
    calls: list[tuple[tuple[str, ...], date, date, Adjustment]] = field(default_factory=list)
    # How many more calls leave out the `hidden` bars; None = every call.
    hide_for_calls: int | None = None

    def add(
        self,
        symbol: str,
        days: Iterable[date],
        price: float = 50.0,
        volume: int = 100_000,
        step: float = 0.0,
    ) -> None:
        series = self.raw.setdefault(symbol, {})
        for i, day in enumerate(days):
            p = price + step * i
            series[day] = DailyBar(symbol, day, p, p * 1.02, p * 0.98, p * 1.01, volume)

    def set_bar(self, symbol: str, day: date, **values: float) -> None:
        self.raw[symbol][day] = replace(self.raw[symbol][day], **values)  # type: ignore[arg-type]

    def daily_bars(
        self,
        symbols: Iterable[str],
        start: date,
        end: date,
        *,
        adjustment: Adjustment = Adjustment.SPLIT,
    ) -> dict[str, list[DailyBar]]:
        wanted = tuple(sorted(set(symbols)))
        self.calls.append((wanted, start, end, adjustment))
        hide = self.hide_for_calls is None or self.hide_for_calls > 0
        if self.hide_for_calls is not None:
            self.hide_for_calls -= 1
        out: dict[str, list[DailyBar]] = {}
        for symbol in wanted:
            bars = []
            for day, b in sorted(self.raw.get(symbol, {}).items()):
                if not start <= day <= end or (hide and (symbol, day) in self.hidden):
                    continue
                if adjustment is Adjustment.SPLIT and symbol in self.splits:
                    ex_date, ratio = self.splits[symbol]
                    if day < ex_date:
                        b = DailyBar(
                            symbol,
                            day,
                            b.open / ratio,
                            b.high / ratio,
                            b.low / ratio,
                            b.close / ratio,
                            round(b.volume * ratio),
                        )
                bars.append(b)
            if bars:
                out[symbol] = bars
        return out

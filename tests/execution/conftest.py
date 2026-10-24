"""A trading world for the job and chaos tests: the test Postgres with a calendar, tickers and the
kill switch off, the fake broker, the real advisory lock, and a job context in paper mode."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
import structlog
from sqlalchemy import Engine, insert, update

from auratrade.config import Regime, RiskConfig, Settings, TradingMode, load_risk_config
from auratrade.db.flags import TRADING_ENABLED
from auratrade.db.models import (
    DailyBar,
    MarketRegimeRow,
    MarketSession,
    Signal,
    SystemFlag,
    Ticker,
)
from auratrade.execution.lock import OrderLock
from auratrade.execution.runtime import ExecutionDeps
from auratrade.jobs.base import NY, JobContext, JobSpec
from auratrade.regime.state import RegimeState
from auratrade.sim.fake_broker import FakeBroker
from auratrade.storage import LocalStore

# Tue 2026-10-20 .. Fri 10-23 are the sessions the tests trade on; 10-22 is "today".
TODAY = date(2026, 10, 22)
SYMBOLS = ("NVDA", "AMD", "MSFT", "AAPL", "META")


def ny(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=NY).astimezone(UTC)


@dataclass
class Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Alerts:
    texts: list[str] = field(default_factory=list)

    def alert(self, text: str) -> None:
        self.texts.append(text)


@dataclass
class World:
    engine: Engine
    url: str
    risk: RiskConfig
    fake: FakeBroker
    clock: Clock
    alerts: Alerts
    storage: LocalStore
    tickers: dict[str, int]
    lock: OrderLock

    def deps(self) -> ExecutionDeps:
        return ExecutionDeps(
            engine=self.engine,
            broker=self.fake,
            prices=self.fake,
            lock=self.lock,
            alerter=self.alerts,
            sleep=self.fake.sleep,
            clock=self.clock,
        )

    def context(
        self, job: str, *, session: date = TODAY, mode: TradingMode = TradingMode.PAPER
    ) -> JobContext:
        settings = Settings(_env_file=None, trading_mode=mode)  # type: ignore[call-arg]
        return JobContext(
            job=job,
            session_date=session,
            run_id=uuid.uuid4().hex,
            trading_mode=mode,
            settings=settings,
            risk=self.risk,
            storage=self.storage,
            started_at=self.clock(),
            log=structlog.get_logger(job),
        )

    def run(
        self,
        make_job: Callable[..., JobSpec],
        *,
        session: date = TODAY,
        mode: TradingMode = TradingMode.PAPER,
    ) -> dict[str, Any]:
        spec = make_job(lambda _ctx: self.deps())
        out = spec.run(self.context(spec.name, session=session, mode=mode))
        return dict(out or {})

    def signal(
        self,
        symbol: str,
        *,
        rank: int = 1,
        trigger: float = 100.0,
        limit: float = 102.0,
        stop: float = 95.0,
        session: date = TODAY,
        risk_fraction: float = 0.01,
    ) -> int:
        with self.engine.begin() as conn:
            return int(
                conn.execute(
                    insert(Signal)
                    .values(
                        idempotency_key=f"{session}:{symbol}:ENTRY:1",
                        session_date=session,
                        decided_session=session - timedelta(days=1),
                        ticker_id=self.tickers[symbol],
                        symbol=symbol,
                        source="SKELETON",
                        strategy="VCP_BREAKOUT",
                        rank=rank,
                        pivot=trigger,
                        trigger_price=trigger,
                        limit_price=limit,
                        stop_price=stop,
                        risk_fraction=risk_fraction,
                    )
                    .returning(Signal.id)
                ).scalar_one()
            )

    def bars(self, symbol: str, closes: list[float], *, last: date = date(2026, 10, 21)) -> None:
        """Daily bars ending on `last`, one per weekday (the test calendar has no holidays)."""
        days: list[date] = []
        day = last
        while len(days) < len(closes):
            if day.weekday() < 5:
                days.append(day)
            day -= timedelta(days=1)
        days.reverse()
        rows = [
            {
                "ticker_id": self.tickers[symbol],
                "session_date": d,
                "open": c,
                "high": c * 1.01,
                "low": c * 0.99,
                "close": c,
                "raw_close": c,
                "volume": 1_000_000,
            }
            for d, c in zip(days, closes, strict=True)
        ]
        with self.engine.begin() as conn:
            conn.execute(insert(DailyBar).values(rows))

    def regime(self, day: date, label: str = "CONFIRMED_UPTREND") -> None:
        """A computed regime row for ``day`` (the scan selects tonight's list by it)."""
        with self.engine.begin() as conn:
            conn.execute(
                insert(MarketRegimeRow).values(
                    session_date=day,
                    index_trend=1,
                    breadth=1,
                    volatility=0,
                    distribution=0,
                    leadership=0,
                    score=2,
                    computed_regime=label,
                    effective_regime=label,
                    crisis=False,
                    follow_through_day=False,
                    no_ftd=False,
                    sessions_in_state=5,
                    expiration_session=False,
                    state=RegimeState(Regime(label), seq=10, entered_seq=5).to_json(),
                    risk_hash=self.risk.config_hash,
                )
            )

    def set_flag(self, name: str, value: bool) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(SystemFlag).where(SystemFlag.name == name).values(value=value))

    def set_session(self, day: date, **values: Any) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(MarketSession).where(MarketSession.session_date == day).values(**values)
            )


def _sessions(first: date, last: date) -> list[dict[str, Any]]:
    out = []
    day = first
    while day <= last:
        if day.weekday() < 5:
            out.append(
                {
                    "session_date": day,
                    "open_at": ny(day, 9, 30),
                    "close_at": ny(day, 16),
                    "is_early_close": False,
                    "minutes": 390,
                    "data_status": "COMPLETE",
                    "calendar_check": "AGREE",
                }
            )
        day += timedelta(days=1)
    return out


@pytest.fixture
def world(pg_engine: Engine, migrated_url: str, tmp_path: Path) -> World:
    risk = load_risk_config()
    with pg_engine.begin() as conn:
        conn.execute(insert(MarketSession).values(_sessions(date(2025, 1, 1), date(2026, 12, 31))))
        ids = {
            s: int(
                conn.execute(
                    insert(Ticker)
                    .values(symbol=s, security_type="COMMON", exchange="NASDAQ")
                    .returning(Ticker.id)
                ).scalar_one()
            )
            for s in SYMBOLS
        }
        conn.execute(
            update(SystemFlag).where(SystemFlag.name == TRADING_ENABLED).values(value=True)
        )
    fake = FakeBroker(cash=100_000.0, now=ny(TODAY, 9, 36))
    for symbol in SYMBOLS:
        fake.set_price(symbol, 99.0)
    clock = Clock(ny(TODAY, 9, 36))
    alerts = Alerts()
    lock = OrderLock(migrated_url, risk.execution, alert=alerts.alert, poll_seconds=0.05)
    return World(
        engine=pg_engine,
        url=migrated_url,
        risk=risk,
        fake=fake,
        clock=clock,
        alerts=alerts,
        storage=LocalStore(tmp_path / "storage"),
        tickers=ids,
        lock=lock,
    )

"""The `ingest` job end to end on Postgres, every vendor faked."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import structlog
from sqlalchemy import Engine, select

from auratrade.calendar.sessions import DbCalendar
from auratrade.calendar.sources import SessionTimes
from auratrade.config import Settings, TradingMode
from auratrade.data.alpaca_data import Asset, CorporateAction, CorporateActionKind
from auratrade.data.edgar import CompanyFacts, CompanyTicker, Submissions
from auratrade.data.finnhub import CalendarEntry, Profile, Surprise
from auratrade.data.macro import CboeIndex
from auratrade.data.nasdaq import Listing
from auratrade.db.models import DailyBar, MacroObservation, MarketSession, Ticker
from auratrade.ingest.universe import UniverseError
from auratrade.jobs import ingest as ingest_job
from auratrade.jobs.base import JobContext, JobRunner, RunnerDeps
from auratrade.jobs.runs import InMemoryJobRunStore, JobRun, JobStatus
from auratrade.storage import LocalStore
from tests.calendar_fixture import alpaca_times
from tests.ingest.fakes import FakeBars, small_risk
from tests.ingest.test_earnings import FakeNews
from tests.ingest.test_macro_actions import FakeCboe, et, quote
from tests.jobs.conftest import FakeClock, RecordingAlerter, RecordingInvoker

THU, FRI, SAT = date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 17)
THU_EVENING = et(THU, 17, 0)
RISK = small_risk()
ASSETS = [
    ("SPY", "ETF", "ARCA", "State Street SPDR S&P 500 ETF Trust", True),
    ("QQQ", "ETF", "NASDAQ", "Invesco QQQ Trust, Series 1", True),
    ("AAA", "COMMON", "NASDAQ", "AAA Inc. - Common Stock", False),
    ("BBB", "COMMON", "NYSE", "BBB Corp Common Stock", False),
    ("PENNY", "COMMON", "NASDAQ", "Penny Inc. - Common Stock", False),
]


def aid(i: int) -> str:
    return f"00000000-0000-0000-0000-{i:012d}"


@dataclass
class FakeAlpaca(FakeBars):
    asset_list: list[Asset] = field(default_factory=list)
    actions: list[CorporateAction] = field(default_factory=list)

    def assets(self, *, status: str | None = "active") -> list[Asset]:
        return self.asset_list

    def corporate_actions(
        self,
        start: date,
        end: date,
        *,
        symbols: Iterable[str] | None = None,
        kinds: Iterable[CorporateActionKind] = (),
    ) -> list[CorporateAction]:
        return self.actions


@dataclass
class FakeDirectory:
    rows: list[Listing]

    def listings(self) -> list[Listing]:
        return self.rows


@dataclass
class FakeEdgarAll:
    error: Exception | None = None

    def company_tickers(self) -> list[CompanyTicker]:
        if self.error is not None:
            raise self.error
        return [CompanyTicker(1, "AAA", "AAA"), CompanyTicker(2, "BBB", "BBB")]

    def submissions(self, cik: int, *, since: date | None = None) -> Submissions | None:
        return Submissions(cik, "Co", "3674" if cik == 1 else "6022", None, (), (), ())

    def company_facts(self, cik: int) -> CompanyFacts | None:
        return None


@dataclass
class FakeFinnhubAll:
    def earnings_calendar(self, day: date) -> list[CalendarEntry]:
        if day == date(2026, 10, 27):
            return [CalendarEntry("AAA", day, "AFTER_CLOSE", 2026, 3, 1.0, None, 10, None)]
        return []

    def earnings_surprises(self, symbol: str) -> list[Surprise]:
        return []

    def profile(self, symbol: str) -> Profile | None:
        return None


def sources(
    calendar_check: list[SessionTimes] | None = None,
    *,
    assets: list[Asset] | None = None,
    cboe: FakeCboe | None = None,
    edgar: FakeEdgarAll | None = None,
) -> ingest_job.Sources:
    times = alpaca_times()
    alpaca = FakeAlpaca(
        asset_list=assets
        if assets is not None
        else [
            Asset(aid(i), sym, name, exchange, "active", True, ())
            for i, (sym, _, exchange, name, _) in enumerate(ASSETS)
        ]
    )
    days = [t.day for t in times if date(2026, 8, 1) <= t.day <= date(2026, 12, 31)]
    for sym, *_ in ASSETS:
        alpaca.add(
            sym, days, price=2.0 if sym == "PENNY" else 50.0, step=0.0 if sym == "PENNY" else 0.1
        )
    final = {i: quote(i, 16.0 + n, et(THU, 16, 15, 1)) for n, i in enumerate(CboeIndex)}
    return ingest_job.Sources(
        alpaca=alpaca,
        news=FakeNews(),
        nasdaq=FakeDirectory([Listing(s, n, e, etf, False) for s, _, e, n, etf in ASSETS]),
        cboe=cboe if cboe is not None else FakeCboe(final),
        calendar_primary=lambda start, end: [t for t in times if start <= t.day <= end],
        calendar_check=lambda start, end: [
            t for t in (calendar_check or times) if start <= t.day <= end
        ],
        fred=None,
        edgar=edgar if edgar is not None else FakeEdgarAll(),
        finnhub=FakeFinnhubAll(),
    )


def context(session: date, tmp_path: Path) -> JobContext:
    return JobContext(
        job="ingest",
        session_date=session,
        run_id="r1",
        trading_mode=TradingMode.DRY_RUN,
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        risk=RISK,
        storage=LocalStore(tmp_path),
        started_at=datetime(2026, 10, 15, 21, 0, tzinfo=UTC),
        log=structlog.get_logger("test"),
    )


def job(
    engine: Engine,
    src: ingest_job.Sources,
    alerter: RecordingAlerter,
    now: datetime = THU_EVENING,
) -> Any:
    return ingest_job.make_job(
        engine_factory=lambda _: engine,
        sources_factory=lambda *_: src,
        alerter_factory=lambda _: alerter,
        clock=lambda: now,
    )


class TestJob:
    def test_full_evening_then_a_no_op(self, pg_engine: Engine, tmp_path: Path) -> None:
        alerter = RecordingAlerter()
        src = sources()
        details = job(pg_engine, src, alerter).run(context(THU, tmp_path))
        assert details["failed_steps"] == []
        assert details["universe"]["inserted"] == 5
        assert details["bars"]["tracked"] == 4  # AAA, BBB + SPY, QQQ (PENNY is $2)
        assert details["bars"]["data_status"] == "COMPLETE"
        assert details["macro"]["sources"] == {"VIX": "CBOE_QUOTE", "VIX3M": "CBOE_QUOTE"}
        assert details["companies"]["industry_coverage"] == 1.0
        assert details["earnings"]["upcoming"] == 1
        assert "db_size_mb" in details["retention"]
        assert alerter.texts == []
        with pg_engine.connect() as conn:
            tickers = {r.symbol: r for r in conn.execute(select(Ticker.__table__))}
            assert tickers["SPY"].security_type == "ETF"
            assert tickers["AAA"].sic == 3674
            status = conn.execute(
                select(MarketSession.data_status).where(MarketSession.session_date == THU)
            ).scalar_one()
            assert status == "COMPLETE"
            assert conn.execute(select(MacroObservation.series)).scalars().all()

        again = job(pg_engine, src, alerter).run(context(THU, tmp_path))
        assert again["universe"]["inserted"] == again["universe"]["updated"] == 0
        assert again["bars"]["rows_written"] == 0
        assert again["macro"]["written"] == 0
        assert again["companies"]["assigned"] == 0
        assert again["calendar"] == {
            "inserted": 0,
            "updated": 0,
            "early_close": False,
            "minutes": 390,
        }
        with pg_engine.connect() as conn:
            assert conn.execute(select(DailyBar.ticker_id)).all()

    def test_calendar_disagreement_alerts(self, pg_engine: Engine, tmp_path: Path) -> None:
        alerter = RecordingAlerter()
        times = alpaca_times()
        check = [
            replace(t, close_at=t.close_at.replace(hour=17)) if t.day == FRI else t for t in times
        ]
        job(pg_engine, sources(check), alerter).run(context(THU, tmp_path))
        assert len(alerter.texts) == 1
        assert "2026-10-16 TIMES_DIFFER" in alerter.texts[0]
        with pg_engine.connect() as conn:
            check_result = conn.execute(
                select(MarketSession.calendar_check).where(MarketSession.session_date == FRI)
            ).scalar_one()
        assert check_result == "DISAGREE"

    def test_degraded_steps_alert_and_the_job_succeeds(
        self, pg_engine: Engine, tmp_path: Path
    ) -> None:
        alerter = RecordingAlerter()
        src = sources(
            cboe=FakeCboe(error=RuntimeError("cdn down")),
            edgar=FakeEdgarAll(error=RuntimeError("sec down")),
        )
        details = job(pg_engine, src, alerter).run(context(THU, tmp_path))
        assert details["macro"]["sources"] == {"VIX": None, "VIX3M": None}
        assert details["companies"]["errors"] == ["company_tickers: RuntimeError: sec down"]
        assert any("no value for VIX, VIX3M" in t for t in alerter.texts)

    def test_vanishing_asset_list_fails_closed(self, pg_engine: Engine, tmp_path: Path) -> None:
        alerter = RecordingAlerter()
        job(pg_engine, sources(), alerter).run(context(THU, tmp_path))
        risk = RISK.model_copy(
            update={"ingest": RISK.ingest.model_copy(update={"max_delistings": 1})}
        )
        ctx = replace(context(FRI, tmp_path), risk=risk)
        with pytest.raises(UniverseError):
            job(pg_engine, sources(assets=[]), alerter, et(FRI, 17, 0)).run(ctx)

    def test_bars_are_not_taken_before_they_are_final(
        self, pg_engine: Engine, tmp_path: Path
    ) -> None:
        with pytest.raises(ingest_job.IngestError, match="not final until"):
            job(pg_engine, sources(), RecordingAlerter(), et(THU, 16, 10)).run(
                context(THU, tmp_path)
            )


class TestRunner:
    def deps(
        self, engine: Engine, src: ingest_job.Sources, store: InMemoryJobRunStore, now: datetime
    ) -> RunnerDeps:
        return RunnerDeps(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            risk=RISK,
            calendar=DbCalendar(engine),
            store=store,
            alerter=RecordingAlerter(),
            invoker=RecordingInvoker(),
            storage=LocalStore(Path()),
            clock=FakeClock(now),
        )

    def test_weekend_is_skipped_and_a_session_runs_after_post_market(
        self, pg_engine: Engine, tmp_path: Path
    ) -> None:
        from tests.ingest.fakes import sync_calendar

        sync_calendar(pg_engine)
        src = sources()
        spec = job(pg_engine, src, RecordingAlerter())
        store = InMemoryJobRunStore()
        sat = JobRunner(self.deps(pg_engine, src, store, et(SAT, 17, 0))).run(spec, session=SAT)
        assert sat.status is JobStatus.SKIPPED_MARKET_CLOSED

        store.record(
            JobRun(
                "p1",
                "post_market",
                THU,
                JobStatus.SUCCEEDED,
                et(THU, 16, 20),
                "dry_run",
                "h",
                et(THU, 16, 25),
            )
        )
        deps = replace(
            self.deps(pg_engine, src, store, et(THU, 17, 0)), storage=LocalStore(tmp_path)
        )
        invoker = RecordingInvoker()
        deps = replace(deps, invoker=invoker)
        thu = JobRunner(deps).run(spec, session=THU)
        assert thu.status is JobStatus.SUCCEEDED
        assert invoker.calls == [("simulate", THU, True)]  # the default mode is dry_run


class TestSources:
    def test_built_from_settings(self) -> None:
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            alpaca_api_key="k",
            alpaca_secret_key="s",
            edgar_user_agent="AuraTrade test@example.com",
        )
        src = ingest_job.build_sources(settings, RISK)
        assert src.edgar is not None
        assert (src.fred, src.finnhub) == (None, None)

    def test_alpaca_keys_are_required(self) -> None:
        with pytest.raises(ingest_job.IngestError, match="ALPACA_API_KEY"):
            ingest_job.build_sources(Settings(_env_file=None), RISK)  # type: ignore[call-arg]


def test_a_session_closed_by_the_resync_fails(pg_engine: Engine, tmp_path: Path) -> None:
    times = [t for t in alpaca_times() if t.day != THU]  # an unscheduled closure tonight
    src = replace(
        sources(times), calendar_primary=lambda s, e: [t for t in times if s <= t.day <= e]
    )
    with pytest.raises(ingest_job.IngestError, match="no longer a session"):
        job(pg_engine, src, RecordingAlerter()).run(context(THU, tmp_path))


def test_missing_vendors_are_skipped(pg_engine: Engine, tmp_path: Path) -> None:
    src = replace(sources(), edgar=None, finnhub=None)
    details = job(pg_engine, src, RecordingAlerter()).run(context(THU, tmp_path))
    assert details["companies"] == {"skipped": "EDGAR_USER_AGENT not set"}
    assert details["earnings"] == {"skipped": "FINNHUB_API_KEY or EDGAR_USER_AGENT not set"}
    assert "fundamentals" not in details

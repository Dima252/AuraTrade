"""The `ingest` job end to end on Postgres, every vendor faked: the ETF list's bars, its
corporate actions, macro and retention (task 5.2)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import structlog
from sqlalchemy import Engine, select, text

from auratrade.calendar.sessions import DbCalendar
from auratrade.calendar.sources import SessionTimes
from auratrade.config import Settings, TradingMode
from auratrade.data.alpaca_data import CorporateAction, CorporateActionKind
from auratrade.data.macro import CboeIndex
from auratrade.db.models import CorporateAction as CorporateActionRow
from auratrade.db.models import DailyBar, MacroObservation, MarketSession
from auratrade.jobs import ingest as ingest_job
from auratrade.jobs.base import JobContext, JobRunner, RunnerDeps
from auratrade.jobs.runs import InMemoryJobRunStore, JobRun, JobStatus
from auratrade.storage import LocalStore
from tests.calendar_fixture import alpaca_times
from tests.ingest.fakes import FUNDS, FakeBars, add_ticker, small_risk
from tests.ingest.test_macro_actions import FakeCboe, et, quote
from tests.jobs.conftest import FakeClock, RecordingAlerter, RecordingInvoker

THU, FRI, SAT = date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 17)
THU_EVENING = et(THU, 17, 0)
RISK = small_risk()


@dataclass
class FakeAlpaca(FakeBars):
    actions: list[CorporateAction] = field(default_factory=list)
    action_symbols: list[tuple[str, ...] | None] = field(default_factory=list)

    def corporate_actions(
        self,
        start: date,
        end: date,
        *,
        symbols: Iterable[str] | None = None,
        kinds: Iterable[CorporateActionKind] = (),
    ) -> list[CorporateAction]:
        self.action_symbols.append(None if symbols is None else tuple(symbols))
        return [a for a in self.actions if symbols is None or a.symbol in set(symbols)]


@pytest.fixture
def tickers(pg_engine: Engine) -> dict[str, int]:
    with pg_engine.begin() as conn:
        return {s: add_ticker(conn, s) for s in FUNDS}


def sources(
    calendar_check: list[SessionTimes] | None = None,
    *,
    cboe: FakeCboe | None = None,
    hidden: Iterable[tuple[str, date]] = (),
) -> ingest_job.Sources:
    times = alpaca_times()
    alpaca = FakeAlpaca(hidden=set(hidden))
    days = [t.day for t in times if date(2026, 8, 1) <= t.day <= date(2026, 12, 31)]
    for symbol in FUNDS:
        alpaca.add(symbol, days, price=50.0, step=0.1)
    final = {i: quote(i, 16.0 + n, et(THU, 16, 15, 1)) for n, i in enumerate(CboeIndex)}
    return ingest_job.Sources(
        alpaca=alpaca,
        cboe=cboe if cboe is not None else FakeCboe(final),
        calendar_primary=lambda start, end: [t for t in times if start <= t.day <= end],
        calendar_check=lambda start, end: [
            t for t in (calendar_check or times) if start <= t.day <= end
        ],
        fred=None,
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


@pytest.mark.usefixtures("tickers")
class TestJob:
    def test_full_evening_then_a_no_op(self, pg_engine: Engine, tmp_path: Path) -> None:
        alerter = RecordingAlerter()
        src = sources()
        details = job(pg_engine, src, alerter).run(context(THU, tmp_path))
        assert details["failed_steps"] == []
        assert set(details) >= {"calendar", "bars", "corporate_actions", "macro", "retention"}
        assert not {"universe", "companies", "earnings", "fundamentals"} & set(details)
        assert details["bars"]["tracked"] == len(FUNDS)
        assert details["bars"]["data_status"] == "COMPLETE"
        assert details["macro"]["sources"] == {"VIX": "CBOE_QUOTE", "VIX3M": "CBOE_QUOTE"}
        assert "db_size_mb" in details["retention"]
        assert alerter.texts == []
        with pg_engine.connect() as conn:
            status = conn.execute(
                select(MarketSession.data_status).where(MarketSession.session_date == THU)
            ).scalar_one()
            assert status == "COMPLETE"
            assert conn.execute(select(MacroObservation.series)).scalars().all()

        again = job(pg_engine, src, alerter).run(context(THU, tmp_path))
        assert again["bars"]["rows_written"] == 0
        assert again["macro"]["written"] == 0
        assert again["calendar"] == {
            "inserted": 0,
            "updated": 0,
            "early_close": False,
            "minutes": 390,
        }
        with pg_engine.connect() as conn:
            assert conn.execute(select(DailyBar.ticker_id)).all()

    def test_corporate_actions_of_the_etf_list_only(
        self, pg_engine: Engine, tmp_path: Path
    ) -> None:
        src = sources()
        alpaca = src.alpaca
        assert isinstance(alpaca, FakeAlpaca)
        ex = date(2026, 10, 20)
        alpaca.actions = [
            CorporateAction(CorporateActionKind.SPLIT, "SSO", ex, None, 2.0, "a1", {}),
            CorporateAction(CorporateActionKind.SPLIT, "AAPL", ex, None, 4.0, "a2", {}),
        ]
        details = job(pg_engine, src, RecordingAlerter()).run(context(THU, tmp_path))
        assert alpaca.action_symbols == [FUNDS]
        assert details["corporate_actions"]["upcoming_splits"] == ["SSO 2026-10-20 x2"]
        with pg_engine.connect() as conn:
            stored = conn.execute(select(CorporateActionRow.symbol)).scalars().all()
        assert stored == ["SSO"]

    def test_a_missing_spy_bar_is_incomplete_and_alerts(
        self, pg_engine: Engine, tmp_path: Path
    ) -> None:
        alerter = RecordingAlerter()
        details = job(pg_engine, sources(hidden=[("SPY", THU)]), alerter).run(
            context(THU, tmp_path)
        )
        assert details["bars"]["data_status"] == "INCOMPLETE"
        [text_] = alerter.texts
        assert "bars INCOMPLETE (no bar for SPY)" in text_

    def test_another_fund_without_a_bar_alerts(self, pg_engine: Engine, tmp_path: Path) -> None:
        alerter = RecordingAlerter()
        details = job(pg_engine, sources(hidden=[("GLD", THU)]), alerter).run(
            context(THU, tmp_path)
        )
        assert details["bars"]["data_status"] == "COMPLETE"
        [text_] = alerter.texts
        assert "no bar for GLD" in text_

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

    def test_a_degraded_step_alerts_and_the_job_succeeds(
        self, pg_engine: Engine, tmp_path: Path
    ) -> None:
        alerter = RecordingAlerter()
        src = sources(cboe=FakeCboe(error=RuntimeError("cdn down")))
        details = job(pg_engine, src, alerter).run(context(THU, tmp_path))
        assert details["macro"]["sources"] == {"VIX": None, "VIX3M": None}
        assert any("no value for VIX, VIX3M" in t for t in alerter.texts)

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

    @pytest.mark.usefixtures("tickers")
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
            _env_file=None, alpaca_api_key="k", alpaca_secret_key="s", fred_api_key="f"
        )
        src = ingest_job.build_sources(settings, RISK)
        assert src.fred is not None
        bare = Settings(_env_file=None, alpaca_api_key="k", alpaca_secret_key="s")  # type: ignore[call-arg]
        assert ingest_job.build_sources(bare, RISK).fred is None

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


def test_step_transactions_may_idle_past_neons_limit(pg_engine: Engine) -> None:
    # Neon ends a transaction idle for 5 minutes; a first fill's bar fetch takes about 15.
    show = text("SHOW idle_in_transaction_session_timeout")
    with ingest_job._transaction(pg_engine, RISK) as conn:
        assert conn.execute(show).scalar() == f"{RISK.ingest.transaction_idle_minutes}min"
    with pg_engine.connect() as conn:  # raised for that transaction only
        assert conn.execute(show).scalar() != f"{RISK.ingest.transaction_idle_minutes}min"

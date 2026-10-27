"""The watchdog: missing heartbeats, stuck runs, disabled schedules, alerts once per session and
the dead-man's-switch ping. The job itself runs against the test Postgres."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import structlog
from sqlalchemy import Engine, insert

from auratrade.config import Settings, TradingMode, load_risk_config
from auratrade.db.models import JobRunRow, MarketSession
from auratrade.jobs import schedule, watchdog
from auratrade.jobs.base import JobContext
from auratrade.jobs.schedule import NY, Expected
from auratrade.storage import LocalStore

DAY = date(2026, 10, 22)  # a Thursday
HALF_DAY = date(2026, 10, 23)
SATURDAY = date(2026, 10, 24)
PING_URL = "https://hc-ping.example/secret-uuid"


def ny(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=NY).astimezone(UTC)


# --- missing_heartbeats ----------------------------------------------------------------------


def test_a_heartbeat_is_missing_only_once_past_due() -> None:
    expected = [Expected("execute_open", ny(DAY, 9, 20), ny(DAY, 9, 35))]
    assert watchdog.missing_heartbeats(expected, [], ny(DAY, 9, 34)) == []
    assert watchdog.missing_heartbeats(expected, [], ny(DAY, 9, 35)) == [
        "missing: execute_open 09:20"
    ]


def test_an_at_start_heartbeat_needs_a_run_from_its_slot_on() -> None:
    expected = [
        Expected("execute_open", ny(DAY, 9, 20), ny(DAY, 9, 35)),
        Expected("execute_open", ny(DAY, 9, 31), ny(DAY, 9, 46)),
    ]
    runs = [("execute_open", ny(DAY, 9, 20) + timedelta(seconds=3))]
    assert watchdog.missing_heartbeats(expected, runs, ny(DAY, 10)) == [
        "missing: execute_open 09:31"
    ]
    runs.append(("execute_open", ny(DAY, 9, 31)))
    assert watchdog.missing_heartbeats(expected, runs, ny(DAY, 10)) == []


def test_a_by_end_heartbeat_takes_any_run_of_its_job() -> None:
    expected = [Expected("ingest", None, ny(DAY, 17, 45))]
    assert watchdog.missing_heartbeats(expected, [("simulate", ny(DAY, 17))], ny(DAY, 18)) == [
        "missing: ingest by 17:45"
    ]
    assert watchdog.missing_heartbeats(expected, [("ingest", ny(DAY, 16, 25))], ny(DAY, 18)) == []


# --- the job ----------------------------------------------------------------------------------


@dataclass
class Alerts:
    texts: list[str] = field(default_factory=list)

    def alert(self, text: str) -> None:
        self.texts.append(text)


@dataclass
class Pings:
    urls: list[str] = field(default_factory=list)
    error: Exception | None = None

    def __call__(self, url: str) -> None:
        if self.error is not None:
            raise self.error
        self.urls.append(url)


@dataclass
class Watch:
    engine: Engine
    tmp_path: Path
    alerts: Alerts = field(default_factory=Alerts)
    pings: Pings = field(default_factory=Pings)
    states: list[tuple[str, str]] = field(default_factory=list)
    now: datetime = field(default_factory=lambda: ny(DAY, 10))

    def run(self, *, session: date = DAY, ping_url: str | None = PING_URL) -> dict[str, Any]:
        spec = watchdog.make_job(
            engine_factory=lambda _s: self.engine,
            alerter_factory=lambda _s: self.alerts,
            schedule_states=lambda _s: self.states,
            ping=self.pings,
            clock=lambda: self.now,
        )
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None, trading_mode="paper", healthchecks_ping_url=ping_url
        )
        run_id = uuid.uuid4().hex
        ctx = JobContext(
            job=spec.name,
            session_date=session,
            run_id=run_id,
            trading_mode=TradingMode.PAPER,
            settings=settings,
            risk=load_risk_config(),
            storage=LocalStore(self.tmp_path / "storage"),
            started_at=self.now,
            log=structlog.get_logger(spec.name),
        )
        out = dict(spec.run(ctx) or {})
        # Recorded as the runner would, so the next run sees what was reported.
        self.record("watchdog", self.now, session=session, details=out)
        return out

    def record(
        self,
        job: str,
        started_at: datetime,
        *,
        session: date = DAY,
        status: str = "SUCCEEDED",
        details: dict[str, Any] | None = None,
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(JobRunRow).values(
                    run_id=uuid.uuid4().hex,
                    job=job,
                    session_date=session,
                    status=status,
                    started_at=started_at,
                    finished_at=None if status == "RUNNING" else started_at,
                    trading_mode="paper",
                    risk_hash="0" * 64,
                    details=details or {},
                )
            )


@pytest.fixture
def watch(pg_engine: Engine, tmp_path: Path) -> Watch:
    rows = []
    day = date(2026, 10, 1)
    while day <= date(2026, 11, 30):
        if day.weekday() < 5:
            early = day == HALF_DAY
            rows.append(
                {
                    "session_date": day,
                    "open_at": ny(day, 9, 30),
                    "close_at": ny(day, 13 if early else 16),
                    "is_early_close": early,
                    "minutes": 210 if early else 390,
                    "data_status": "COMPLETE",
                    "calendar_check": "AGREE",
                }
            )
        day += timedelta(days=1)
    with pg_engine.begin() as conn:
        conn.execute(insert(MarketSession).values(rows))
    return Watch(pg_engine, tmp_path)


def morning(watch: Watch, day: date = DAY) -> None:
    watch.record("portfolio_manager", ny(day, 8, 45) + timedelta(seconds=2), session=day)
    for hour, minute, job in ((9, 20, "execute_open"), (9, 31, "execute_open")):
        watch.record(job, ny(day, hour, minute) + timedelta(seconds=2), session=day)


def test_all_present_means_no_alert_and_a_ping(watch: Watch) -> None:
    morning(watch)
    out = watch.run()
    assert out == {"session": True, "findings": [], "alerted": [], "pinged": True}
    assert watch.alerts.texts == []
    assert watch.pings.urls == [PING_URL]


def test_a_missing_heartbeat_is_alerted_once_per_session(watch: Watch) -> None:
    watch.record("execute_open", ny(DAY, 9, 20))
    watch.record("execute_open", ny(DAY, 9, 31))
    out = watch.run()
    assert out["alerted"] == ["missing: portfolio_manager 08:45"]
    (text,) = watch.alerts.texts
    assert "[AuraTrade dev] watchdog:" in text
    assert "- missing: portfolio_manager 08:45" in text

    watch.now = ny(DAY, 10, 15)
    out = watch.run()
    assert out["findings"] == ["missing: portfolio_manager 08:45"]
    assert out["alerted"] == []
    assert len(watch.alerts.texts) == 1


def test_a_failed_watchdog_run_still_counts_as_reported(watch: Watch) -> None:
    watch.record("watchdog", ny(DAY, 9, 45), status="FAILED", details={"findings": ["x"]})
    watch.record("watchdog", ny(DAY, 9, 50), status="RUNNING", details={"findings": ["stuck: y"]})
    ctx = cast(JobContext, SimpleNamespace(session_date=DAY, run_id="current"))
    with watch.engine.connect() as conn:
        assert watchdog._reported(conn, ctx) == {"x"}


def test_the_half_day_expects_no_execute_close(watch: Watch) -> None:
    morning(watch, HALF_DAY)
    watch.now = ny(HALF_DAY, 16, 0)
    assert watch.run(session=HALF_DAY)["findings"] == []
    morning(watch)
    watch.now = ny(DAY, 16, 0)
    assert watch.run()["findings"] == ["missing: execute_close 15:40"]


def test_a_stuck_run_is_found_on_any_day(watch: Watch) -> None:
    watch.now = ny(SATURDAY, 10)
    watch.record("ingest", ny(SATURDAY, 9, 30), session=DAY, status="RUNNING")
    watch.record("simulate", ny(SATURDAY, 9, 55), session=DAY, status="RUNNING")
    out = watch.run(session=SATURDAY)
    assert out["session"] is False
    (finding,) = out["findings"]
    assert finding.startswith(f"stuck: ingest {DAY} run ")


def test_a_schedule_not_enabled_is_a_finding(watch: Watch) -> None:
    morning(watch)
    watch.states = [("auratrade-paper-scan-1", "ENABLED"), ("auratrade-paper-ingest-1", "DISABLED")]
    out = watch.run()
    assert out["findings"] == ["schedule disabled: auratrade-paper-ingest-1"]
    assert watch.alerts.texts


def test_a_failed_ping_is_logged_not_raised(watch: Watch) -> None:
    morning(watch)
    watch.pings.error = ConnectionError("no route")
    assert watch.run()["pinged"] is False


def test_no_ping_url_means_no_ping(watch: Watch) -> None:
    morning(watch)
    assert watch.run(ping_url=None)["pinged"] is False
    assert watch.pings.urls == []


def test_the_watchdog_expects_what_the_schedule_says(watch: Watch) -> None:
    risk = load_risk_config()
    expected = schedule.expected_heartbeats(
        schedule.weekday_slots(risk.jobs.chain_wait_minutes),
        DAY,
        early_close=False,
        grace_minutes=risk.jobs.watchdog_grace_minutes,
    )
    watch.now = ny(DAY, 23)
    out = watch.run()
    assert out["findings"] == [f"missing: {e.label}" for e in expected]


# --- the AWS and HTTP edges -----------------------------------------------------------------


class FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.kwargs: dict[str, Any] = {}

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.kwargs = kwargs
        return self.pages


class FakeScheduler:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.paginator = FakePaginator(pages)

    def get_paginator(self, name: str) -> FakePaginator:
        assert name == "list_schedules"
        return self.paginator


def test_schedule_states_need_a_group() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert watchdog.aws_schedule_states(settings) == []


def test_schedule_states_list_the_group(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    fake = FakeScheduler(
        [
            {"Schedules": [{"Name": "a", "State": "ENABLED"}]},
            {"Schedules": [{"Name": "b", "State": "DISABLED"}, {}]},
        ]
    )
    clients: list[str] = []

    def client(service: str) -> FakeScheduler:
        clients.append(service)
        return fake

    monkeypatch.setattr(boto3, "client", client)
    settings = Settings(_env_file=None, schedule_group="auratrade-paper")  # type: ignore[call-arg]
    assert watchdog.aws_schedule_states(settings) == [
        ("a", "ENABLED"),
        ("b", "DISABLED"),
        ("?", "?"),
    ]
    assert clients == ["scheduler"]
    assert fake.paginator.kwargs == {"GroupName": "auratrade-paper"}


def test_http_ping_raises_on_an_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    calls: list[tuple[str, float]] = []

    def get(url: str, *, timeout: float) -> httpx.Response:
        calls.append((url, timeout))
        status = 200 if url.endswith("ok") else 404
        return httpx.Response(status, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    watchdog.http_ping("https://hc-ping.example/ok")
    with pytest.raises(httpx.HTTPStatusError):
        watchdog.http_ping("https://hc-ping.example/gone")
    assert calls[0] == ("https://hc-ping.example/ok", watchdog.PING_TIMEOUT_SECONDS)


def test_the_module_job_is_wired() -> None:
    assert watchdog.JOB.name == "watchdog"
    assert callable(watchdog.handler)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert hasattr(watchdog._alerter(settings), "alert")
    assert watchdog._utc_now().tzinfo is UTC

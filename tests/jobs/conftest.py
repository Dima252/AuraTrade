from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from auratrade.config import RiskConfig, Settings, load_risk_config
from auratrade.jobs.base import NY, JobRunner, RunnerDeps
from auratrade.jobs.runs import InMemoryJobRunStore
from auratrade.log import Redactor
from auratrade.storage import LocalStore

SECRET = "tok-SECRET-1"


@dataclass
class FakeCalendar:
    sessions: set[date] = field(default_factory=set)
    error: Exception | None = None

    def is_session(self, day: date) -> bool:
        if self.error is not None:
            raise self.error
        return day in self.sessions


@dataclass
class FakeClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@dataclass
class RecordingAlerter:
    texts: list[str] = field(default_factory=list)
    error: Exception | None = None

    def alert(self, text: str) -> None:
        self.texts.append(text)
        if self.error is not None:
            raise self.error


@dataclass
class RecordingInvoker:
    calls: list[tuple[str, date, bool]] = field(default_factory=list)
    error: Exception | None = None

    def invoke(self, job: str, session_date: date, *, dry_run: bool) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append((job, session_date, dry_run))


def ny(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NY).astimezone(UTC)


@dataclass
class Harness:
    calendar: FakeCalendar
    clock: FakeClock
    store: InMemoryJobRunStore
    alerter: RecordingAlerter
    invoker: RecordingInvoker
    deps: RunnerDeps

    @property
    def runner(self) -> JobRunner:
        return JobRunner(self.deps)


@pytest.fixture
def risk() -> RiskConfig:
    return load_risk_config()


@pytest.fixture
def make_harness(tmp_path: Path, risk: RiskConfig) -> Callable[..., Harness]:
    def build(
        *, sessions: set[date] | None = None, now: datetime | None = None, mode: str = "paper"
    ) -> Harness:
        calendar = FakeCalendar(sessions or set())
        clock = FakeClock(now or datetime(2026, 10, 7, 21, 0, tzinfo=UTC))
        store = InMemoryJobRunStore()
        alerter = RecordingAlerter()
        invoker = RecordingInvoker()
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None, trading_mode=mode, telegram_bot_token=SECRET
        )
        deps = RunnerDeps(
            settings=settings,
            risk=risk,
            calendar=calendar,
            store=store,
            alerter=alerter,
            invoker=invoker,
            storage=LocalStore(tmp_path / "storage"),
            redactor=Redactor(settings.secret_values()),
            clock=clock,
        )
        return Harness(calendar, clock, store, alerter, invoker, deps)

    return build

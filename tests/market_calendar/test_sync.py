from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine, select, update

from auratrade.calendar import sync as sync_module
from auratrade.calendar.sessions import load_calendar
from auratrade.calendar.sources import SessionTimes
from auratrade.calendar.sync import (
    CalendarSyncError,
    DisagreementKind,
    SyncResult,
    alert_text,
    compare,
    run_sync,
    sync_market_sessions,
)
from auratrade.db.models import MarketSession
from tests.calendar_fixture import alpaca_times

START, END = date(2025, 12, 1), date(2027, 1, 31)
NOW = datetime(2026, 10, 9, 21, 0, tzinfo=UTC)
WED = date(2026, 10, 7)
GOOD_FRIDAY = date(2026, 4, 3)


def without(times: list[SessionTimes], day: date) -> list[SessionTimes]:
    return [s for s in times if s.day != day]


def with_close(times: list[SessionTimes], day: date, hour_utc: int) -> list[SessionTimes]:
    return [
        replace(s, close_at=s.close_at.replace(hour=hour_utc)) if s.day == day else s for s in times
    ]


def sync(engine: Engine, primary: list[SessionTimes], check: list[SessionTimes]) -> SyncResult:
    return sync_market_sessions(engine, primary, check, start=START, end=END, now=NOW)


class TestCompare:
    def test_agreement(self) -> None:
        assert compare(alpaca_times(), alpaca_times()) == []

    def test_kinds(self) -> None:
        alpaca = alpaca_times()
        library = with_close(without(alpaca, WED), date(2026, 10, 8), 17)
        library.append(
            SessionTimes(
                GOOD_FRIDAY,
                datetime(2026, 4, 3, 13, 30, tzinfo=UTC),
                datetime(2026, 4, 3, 20, 0, tzinfo=UTC),
            )
        )
        found = {d.day: d for d in compare(alpaca, library)}
        assert found[WED].kind is DisagreementKind.MISSING_IN_CHECK
        assert found[date(2026, 10, 8)].kind is DisagreementKind.TIMES_DIFFER
        assert "Alpaca 09:30-16:00; library 09:30-13:00 ET" in found[date(2026, 10, 8)].detail
        assert found[GOOD_FRIDAY].kind is DisagreementKind.ONLY_IN_CHECK

    def test_library_beyond_alpaca_horizon_is_ignored(self) -> None:
        alpaca = [s for s in alpaca_times() if s.day <= date(2026, 12, 31)]
        assert compare(alpaca, alpaca_times()) == []
        assert compare([], alpaca_times()) == []


class TestSync:
    def test_first_sync_writes_every_session(self, pg_engine: Engine) -> None:
        result = sync(pg_engine, alpaca_times(), alpaca_times())
        assert (result.sessions, result.inserted, result.updated) == (292, 292, 0)
        assert result.end == date(2027, 1, 29)
        assert alert_text(result) is None
        with pg_engine.connect() as conn:
            cal = load_calendar(conn)
        assert not cal.is_session(GOOD_FRIDAY)
        session = cal.session_for(date(2026, 11, 27))
        assert session is not None
        assert (session.is_early_close, session.minutes, session.calendar_agrees) == (
            True,
            210,
            True,
        )

    def test_second_sync_is_a_no_op(self, pg_engine: Engine) -> None:
        sync(pg_engine, alpaca_times(), alpaca_times())
        again = sync(pg_engine, alpaca_times(), alpaca_times())
        assert not again.changed
        with pg_engine.connect() as conn:
            stamps = set(conn.execute(select(MarketSession.synced_at)).scalars())
        assert stamps == {NOW}

    def test_keeps_ingest_owned_columns(self, pg_engine: Engine) -> None:
        sync(pg_engine, alpaca_times(), alpaca_times())
        with pg_engine.begin() as conn:
            conn.execute(
                update(MarketSession)
                .where(MarketSession.session_date == WED)
                .values(data_status="INCOMPLETE", coverage=0.8)
            )
        sync(pg_engine, with_close(alpaca_times(), WED, 17), alpaca_times())
        with pg_engine.connect() as conn:
            row = conn.execute(select(MarketSession).where(MarketSession.session_date == WED)).one()
        assert (row.data_status, row.coverage) == ("INCOMPLETE", pytest.approx(0.8))

    def test_disagreement_marks_the_session(self, pg_engine: Engine) -> None:
        alpaca = alpaca_times()
        result = sync(pg_engine, alpaca, with_close(alpaca, WED, 17))
        assert [d.day for d in result.disagreements] == [WED]
        text = alert_text(result)
        assert text is not None
        assert "2026-10-07 TIMES_DIFFER" in text
        with pg_engine.connect() as conn:
            cal = load_calendar(conn)
        wed = cal.session_for(WED)
        assert wed is not None
        assert wed.calendar_check == "DISAGREE"
        assert not wed.calendar_agrees
        thu = cal.session_for(date(2026, 10, 8))
        assert thu is not None
        assert thu.calendar_agrees
        # Resolved on the next sync.
        assert sync(pg_engine, alpaca, alpaca).updated == 1

    def test_library_only_date_is_reported_not_written(self, pg_engine: Engine) -> None:
        extra = SessionTimes(
            GOOD_FRIDAY,
            datetime(2026, 4, 3, 13, 30, tzinfo=UTC),
            datetime(2026, 4, 3, 20, 0, tzinfo=UTC),
        )
        result = sync(pg_engine, alpaca_times(), [*alpaca_times(), extra])
        assert [d.kind for d in result.disagreements] == [DisagreementKind.ONLY_IN_CHECK]
        assert "2026-04-03 ONLY_IN_CHECK: Alpaca: closed" in (alert_text(result) or "")
        with pg_engine.connect() as conn:
            assert not load_calendar(conn).is_session(GOOD_FRIDAY)

    def test_unscheduled_closure_is_removed(self, pg_engine: Engine) -> None:
        sync(pg_engine, alpaca_times(), alpaca_times())
        closed = date(2026, 12, 3)
        result = sync(pg_engine, without(alpaca_times(), closed), without(alpaca_times(), closed))
        assert result.removed == (closed,)
        assert "2026-12-03 REMOVED" in (alert_text(result) or "")
        with pg_engine.connect() as conn:
            assert not load_calendar(conn).is_session(closed)

    def test_refuses_to_drop_many_sessions(self, pg_engine: Engine) -> None:
        sync(pg_engine, alpaca_times(), alpaca_times())
        damaged = [s for s in alpaca_times() if s.day.month != 6]
        with pytest.raises(CalendarSyncError, match="nothing written"):
            sync(pg_engine, damaged, damaged)
        with pg_engine.connect() as conn:
            assert load_calendar(conn).is_session(date(2026, 6, 1))

    def test_refuses_an_empty_answer(self, pg_engine: Engine) -> None:
        with pytest.raises(CalendarSyncError, match="no sessions"):
            sync(pg_engine, [], alpaca_times())

    def test_sessions_outside_the_window_are_ignored(self, pg_engine: Engine) -> None:
        result = sync_market_sessions(
            pg_engine,
            alpaca_times(),
            alpaca_times(),
            start=date(2026, 1, 1),
            end=date(2026, 12, 31),
            now=NOW,
        )
        assert result.sessions == 251

    def test_run_sync(self, pg_engine: Engine) -> None:
        def fetch(start: date, end: date) -> list[SessionTimes]:
            return [s for s in alpaca_times() if start <= s.day <= end]

        result = run_sync(pg_engine, fetch, fetch, date(2026, 1, 1), date(2026, 3, 31))
        assert result.inserted == len(fetch(date(2026, 1, 1), date(2026, 3, 31)))


class TestCli:
    def test_missing_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from auratrade import config

        monkeypatch.setattr(
            config,
            "load_settings",
            lambda: config.Settings(_env_file=None),  # type: ignore[call-arg]
        )
        assert sync_module.main([]) == 1

    @pytest.mark.parametrize(("damage", "code"), [(False, 0), (True, 2)])
    def test_sync_exit_codes(
        self, monkeypatch: pytest.MonkeyPatch, pg_engine: Engine, damage: bool, code: int
    ) -> None:
        from auratrade import config
        from auratrade.calendar import sources
        from auratrade.db import engine as db_engine

        settings = config.Settings(  # type: ignore[call-arg]
            _env_file=None, alpaca_api_key="k", alpaca_secret_key="s"
        )
        monkeypatch.setattr(config, "load_settings", lambda: settings)
        monkeypatch.setattr(db_engine, "engine_from_settings", lambda s: _NoDispose(pg_engine))

        class FakeAlpaca:
            def __init__(self, *args: object) -> None:
                pass

            def fetch(self, start: date, end: date) -> list[SessionTimes]:
                return [s for s in alpaca_times() if start <= s.day <= end]

        library = with_close(alpaca_times(), WED, 17) if damage else alpaca_times()
        monkeypatch.setattr(sources, "AlpacaCalendarClient", FakeAlpaca)
        monkeypatch.setattr(
            sources,
            "exchange_calendar_sessions",
            lambda start, end: [s for s in library if start <= s.day <= end],
        )
        assert sync_module.main(["--start", "2026-01-01", "--end", "2026-12-31"]) == code

    def test_sync_error_exit_code(self, monkeypatch: pytest.MonkeyPatch, pg_engine: Engine) -> None:
        from auratrade import config
        from auratrade.calendar import sources
        from auratrade.db import engine as db_engine

        settings = config.Settings(  # type: ignore[call-arg]
            _env_file=None, alpaca_api_key="k", alpaca_secret_key="s"
        )
        monkeypatch.setattr(config, "load_settings", lambda: settings)
        monkeypatch.setattr(db_engine, "engine_from_settings", lambda s: _NoDispose(pg_engine))

        class Down:
            def __init__(self, *args: object) -> None:
                pass

            def fetch(self, start: date, end: date) -> list[SessionTimes]:
                raise RuntimeError("alpaca down")

        monkeypatch.setattr(sources, "AlpacaCalendarClient", Down)
        assert sync_module.main([]) == 1


class _NoDispose:
    """Lets main() 'dispose' the shared test engine without closing it."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def begin(self):  # type: ignore[no-untyped-def]
        return self._engine.begin()

    def dispose(self) -> None:
        pass

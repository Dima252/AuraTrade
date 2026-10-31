"""The regime on Postgres: loading, replay, the evening step and the job."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import pytest
import structlog
import yaml
from sqlalchemy import Engine, delete, insert, orm, select

from auratrade.calendar.sessions import load_calendar
from auratrade.config import Regime, RiskConfig, Settings, TradingMode
from auratrade.db.models import DailyBar, MacroObservation, MarketRegimeRow, MarketSession, Ticker
from auratrade.jobs import regime as regime_job
from auratrade.jobs.base import JobContext
from auratrade.notify.alerts import LogAlerter
from auratrade.regime.compute import catch_up, compute, replay
from auratrade.regime.inputs import MissingDataError
from auratrade.regime.store import load_data, write_results
from auratrade.storage import LocalStore
from tests.jobs.conftest import RecordingAlerter
from tests.regime.regime_helpers import small_risk, weekdays

RISK = small_risk()
SESSIONS = weekdays(date(2026, 6, 1), 40)


def _ticker(conn: Any, symbol: str, **kwargs: Any) -> int:
    return int(
        conn.execute(
            insert(Ticker).values(symbol=symbol, **kwargs).returning(Ticker.id)
        ).scalar_one()
    )


def _bars(conn: Any, ticker_id: int, closes: dict[date, float], volume: int = 1000) -> None:
    conn.execute(
        insert(DailyBar),
        [
            {
                "ticker_id": ticker_id,
                "session_date": d,
                "open": c,
                "high": c,
                "low": c,
                "close": c,
                "raw_close": c,
                "volume": volume,
            }
            for d, c in closes.items()
        ],
    )


def _macro(conn: Any, days: list[date], source: str = "CBOE") -> None:
    conn.execute(
        insert(MacroObservation),
        [
            {"series": s, "observation_date": d, "value": v, "source": source}
            for d in days
            for s, v in (("VIX", 15.0), ("VIX3M", 18.0))
        ],
    )


@pytest.fixture
def seeded(pg_engine: Engine) -> dict[str, int]:
    """40 sessions; SPY and QQQ rising 1% a day; other tickers the regime doesn't load; VIX 15 /
    VIX3M 18."""
    with pg_engine.begin() as conn:
        conn.execute(
            insert(MarketSession),
            [
                {
                    "session_date": d,
                    "open_at": datetime.combine(d, time(13, 30), tzinfo=UTC),
                    "close_at": datetime.combine(d, time(20, 0), tzinfo=UTC),
                    "is_early_close": False,
                    "minutes": 390,
                }
                for d in SESSIONS
            ],
        )
        ids = {
            "SPY": _ticker(conn, "SPY"),
            "QQQ": _ticker(conn, "QQQ"),
        }
        rising = {d: 100.0 * 1.01**k for k, d in enumerate(SESSIONS)}
        _bars(conn, ids["SPY"], rising)
        _bars(conn, ids["QQQ"], rising)
        _macro(conn, SESSIONS)
    return ids


def _rows(engine: Engine) -> list[MarketRegimeRow]:
    with orm.Session(engine) as session:
        return list(session.scalars(select(MarketRegimeRow).order_by(MarketRegimeRow.session_date)))


def _replay(engine: Engine, first: date, last: date, risk: RiskConfig = RISK) -> None:
    with engine.begin() as conn:
        replay(conn, risk, first, last)


class TestLoad:
    def test_only_the_indexes_with_their_look_back(
        self, pg_engine: Engine, seeded: dict[str, int]
    ) -> None:
        with pg_engine.connect() as conn:
            data = load_data(conn, load_calendar(conn), SESSIONS[30], SESSIONS[39], RISK.regime)
        assert set(data.indexes) == {"SPY", "QQQ"}  # no stock universe since task 5.2
        assert data.sessions[0] == SESSIONS[23]  # 7 sessions: the long SMA and its slope


class TestReplayAndStep:
    def test_replay_writes_the_labelled_sessions(
        self, pg_engine: Engine, seeded: dict[str, int]
    ) -> None:
        _replay(pg_engine, SESSIONS[12], SESSIONS[30])
        rows = _rows(pg_engine)
        assert [r.session_date for r in rows] == SESSIONS[12:31]
        assert {r.risk_hash for r in rows} == {RISK.config_hash}
        # Rising indexes, calm VIX and no distribution: +3 every session, no FTD (1% days), so
        # the no_ftd fallback, then CONFIRMED_UPTREND after 10 labels.
        assert {r.score for r in rows if r.session_date >= SESSIONS[20]} == {3}
        regimes = [r.computed_regime for r in rows]
        assert Regime.EARLY_UPTREND.value in regimes
        assert regimes[-1] == Regime.CONFIRMED_UPTREND.value
        assert any(r.no_ftd for r in rows)

    def test_the_evening_step_continues_like_a_replay(
        self, pg_engine: Engine, seeded: dict[str, int]
    ) -> None:
        _replay(pg_engine, SESSIONS[12], SESSIONS[30])
        full = {r.session_date: (r.computed_regime, r.score, r.state) for r in _rows(pg_engine)}
        with pg_engine.begin() as conn:
            conn.execute(delete(MarketRegimeRow))
        _replay(pg_engine, SESSIONS[12], SESSIONS[25])
        for day in SESSIONS[26:31]:
            with pg_engine.begin() as conn:
                catch_up(conn, RISK, day)[-1]
        stepped = {r.session_date: (r.computed_regime, r.score, r.state) for r in _rows(pg_engine)}
        assert stepped == full

    def test_the_step_needs_a_seed(self, pg_engine: Engine, seeded: dict[str, int]) -> None:
        with pytest.raises(MissingDataError, match="seed it"), pg_engine.begin() as conn:
            catch_up(conn, RISK, SESSIONS[30])[-1]

    def test_the_step_fills_missed_evenings(
        self, pg_engine: Engine, seeded: dict[str, int]
    ) -> None:
        _replay(pg_engine, SESSIONS[12], SESSIONS[30])
        full = {r.session_date: (r.computed_regime, r.score, r.state) for r in _rows(pg_engine)}
        with pg_engine.begin() as conn:
            conn.execute(delete(MarketRegimeRow))
        _replay(pg_engine, SESSIONS[12], SESSIONS[25])
        # Five evenings missed: the sixth classifies them on its way, as the steps would have.
        with pg_engine.begin() as conn:
            results = catch_up(conn, RISK, SESSIONS[30])
        assert [r.session for r in results] == list(SESSIONS[26:31])
        stepped = {r.session_date: (r.computed_regime, r.score, r.state) for r in _rows(pg_engine)}
        assert stepped == full

    def test_a_replay_needs_its_warm_up(self, pg_engine: Engine, seeded: dict[str, int]) -> None:
        with pytest.raises(MissingDataError, match="warm-up"):
            _replay(pg_engine, SESSIONS[8], SESSIONS[20])
        with pytest.raises(ValueError, match="not a session"):
            _replay(pg_engine, date(2026, 6, 6), SESSIONS[20])

    def test_a_missing_index_bar_fails_closed(
        self, pg_engine: Engine, seeded: dict[str, int]
    ) -> None:
        _replay(pg_engine, SESSIONS[12], SESSIONS[30])
        with pg_engine.begin() as conn:
            conn.execute(
                delete(DailyBar).where(
                    DailyBar.ticker_id == seeded["QQQ"], DailyBar.session_date == SESSIONS[31]
                )
            )
        with pytest.raises(MissingDataError, match="QQQ"), pg_engine.begin() as conn:
            catch_up(conn, RISK, SESSIONS[31])[-1]


class TestMacro:
    def test_fred_values_fill_in_when_cboe_is_late(
        self, pg_engine: Engine, seeded: dict[str, int]
    ) -> None:
        _replay(pg_engine, SESSIONS[12], SESSIONS[30])
        with pg_engine.begin() as conn:
            conn.execute(
                delete(MacroObservation).where(MacroObservation.observation_date >= SESSIONS[30])
            )
            _macro(conn, [SESSIONS[30]], source="FRED")
            result = catch_up(conn, RISK, SESSIONS[31])[-1]  # one session stale: fine
        assert result.features["macro_stale_sessions"] == 1
        assert not result.crisis.active

    def test_both_sources_stale_trigger_the_crisis_override(
        self, pg_engine: Engine, seeded: dict[str, int]
    ) -> None:
        _replay(pg_engine, SESSIONS[12], SESSIONS[30])
        with pg_engine.begin() as conn:
            conn.execute(
                delete(MacroObservation).where(MacroObservation.observation_date > SESSIONS[30])
            )
            catch_up(conn, RISK, SESSIONS[31])[-1]
            catch_up(conn, RISK, SESSIONS[32])[-1]
            third = catch_up(conn, RISK, SESSIONS[33])[-1]
        assert third.crisis.reasons == ("macro_stale",)
        assert third.pillars.volatility == 0
        assert _rows(pg_engine)[-1].crisis


def _context(session: date, risk: RiskConfig, tmp_path: Path) -> JobContext:
    return JobContext(
        job="regime",
        session_date=session,
        run_id="r1",
        trading_mode=TradingMode.DRY_RUN,
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        risk=risk,
        storage=LocalStore(tmp_path),
        started_at=datetime(2026, 8, 1, tzinfo=UTC),
        log=structlog.get_logger("test"),
    )


class TestJob:
    def test_the_job_writes_the_session_and_alerts_on_stale_macro(
        self, pg_engine: Engine, seeded: dict[str, int], tmp_path: Path
    ) -> None:
        _replay(pg_engine, SESSIONS[12], SESSIONS[30])
        with pg_engine.begin() as conn:
            conn.execute(
                delete(MacroObservation).where(MacroObservation.observation_date > SESSIONS[28])
            )
        alerter = RecordingAlerter()
        spec = regime_job.make_job(
            engine_factory=lambda _: pg_engine, alerter_factory=lambda _: alerter
        )
        details = spec.run(_context(SESSIONS[31], RISK, tmp_path))
        assert details is not None
        assert details["crisis"] == ["macro_stale"]
        assert details["computed_regime"] == Regime.CONFIRMED_UPTREND.value
        assert len(alerter.texts) == 1
        assert "stale in CBOE and FRED" in alerter.texts[0]
        assert _rows(pg_engine)[-1].session_date == SESSIONS[31]

    def test_no_alert_when_macro_is_fresh(
        self, pg_engine: Engine, seeded: dict[str, int], tmp_path: Path
    ) -> None:
        _replay(pg_engine, SESSIONS[12], SESSIONS[30])
        alerter = RecordingAlerter()
        spec = regime_job.make_job(
            engine_factory=lambda _: pg_engine, alerter_factory=lambda _: alerter
        )
        spec.run(_context(SESSIONS[31], RISK, tmp_path))
        assert alerter.texts == []

    def test_the_job_is_in_the_evening_chain(self) -> None:
        assert regime_job.JOB.name == "regime"
        assert regime_job.JOB.predecessor == "simulate"
        assert regime_job.JOB.successor == "allocate"


class TestCli:
    def test_from_runs_a_replay(
        self,
        pg_engine: Engine,
        seeded: dict[str, int],
        migrated_url: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        risk_file = tmp_path / "risk.yaml"
        risk_file.write_text(yaml.safe_dump(RISK.model_dump(mode="json")), encoding="utf-8")
        monkeypatch.setenv("DATABASE_URL", migrated_url)
        monkeypatch.setenv("RISK_CONFIG_PATH", str(risk_file))
        monkeypatch.setattr("auratrade.jobs.wiring.load_runtime_settings", _settings_from_env)
        code = regime_job.main(
            ["--from", SESSIONS[12].isoformat(), "--session", SESSIONS[20].isoformat()]
        )
        assert code == 0
        assert [r.session_date for r in _rows(pg_engine)] == SESSIONS[12:21]

    def test_without_from_it_runs_the_job(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []

        def fake_run_cli(spec: object, argv: list[str]) -> int:
            calls.append(argv)
            return 0

        monkeypatch.setattr(regime_job, "run_cli", fake_run_cli)
        assert regime_job.main(["--session", "2026-10-09", "--dry-run"]) == 0
        assert calls == [["--session", "2026-10-09", "--dry-run"]]


def _settings_from_env() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


class TestEdges:
    def test_an_empty_calendar(self, pg_engine: Engine) -> None:
        with pg_engine.connect() as conn:
            with pytest.raises(MissingDataError, match="empty"):
                replay(conn, RISK, SESSIONS[12], SESSIONS[20])
            with pytest.raises(MissingDataError, match="empty"):
                load_data(conn, load_calendar(conn), SESSIONS[12], SESSIONS[20], RISK.regime)

    def test_nothing_to_compute_or_write(self, pg_engine: Engine) -> None:
        with pg_engine.begin() as conn:
            assert compute(conn, RISK, [], None) == []
            write_results(conn, [], RISK.config_hash)
        assert _rows(pg_engine) == []

    def test_an_index_symbol_with_two_securities_fails_closed(
        self, pg_engine: Engine, seeded: dict[str, int]
    ) -> None:
        with pg_engine.begin() as conn:
            other = _ticker(conn, "SPY", active=False)
            _bars(conn, other, {SESSIONS[30]: 99.0})
        with pytest.raises(MissingDataError, match="several securities"):
            _replay(pg_engine, SESSIONS[12], SESSIONS[30])

    def test_the_default_alerter_is_built_from_settings(self) -> None:
        assert isinstance(regime_job._alerter(_settings_from_env()), LogAlerter)

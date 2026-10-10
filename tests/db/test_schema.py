from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, delete, insert, inspect, select, text, update
from sqlalchemy.exc import IntegrityError

from auratrade.db.engine import make_engine
from auratrade.db.flags import SEED, TRADING_ENABLED, read_flags
from auratrade.db.models import (
    Base,
    CorporateAction,
    DailyBar,
    DetectedBase,
    MarketSession,
    Mismatch,
    SystemFlag,
    Ticker,
)
from tests.pg import REPO_ROOT, alembic_config, temporary_database, upgrade

TABLES = {
    "tickers",
    "industry_groups",
    "market_sessions",
    "daily_bars",
    "corporate_actions",
    "macro_series",
    "fundamentals",
    "earnings_events",
    "job_runs",
    "system_flags",
    "mismatches",
    "bases",
    "market_regime",
}


class TestMigrations:
    def test_head_creates_every_table(self, pg_engine: Engine) -> None:
        assert set(inspect(pg_engine).get_table_names()) == TABLES | {"alembic_version"}

    def test_models_match_the_migrations(self, pg_engine: Engine) -> None:
        with pg_engine.connect() as conn:
            diff = compare_metadata(
                MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata
            )
        assert diff == []

    def test_downgrade_and_upgrade_round_trip(self) -> None:
        with temporary_database("auratrade_migrate") as url:
            upgrade(url)
            command.downgrade(alembic_config(url), "base")
            engine = make_engine(url)
            try:
                assert set(inspect(engine).get_table_names()) == {"alembic_version"}
                upgrade(url)
                assert set(inspect(engine).get_table_names()) >= TABLES
            finally:
                engine.dispose()

    def test_migration_seeds_match_the_code(self) -> None:
        source = (REPO_ROOT / "alembic/versions/0001_initial_data_layer.py").read_text(
            encoding="utf-8"
        )
        seeded = dict(re.findall(r'\{"name": "(\w+)", "value": (True|False)\}', source))
        assert {k: v == "True" for k, v in seeded.items()} == SEED


class TestFlags:
    def test_fresh_database_starts_with_entries_disabled(self, pg_engine: Engine) -> None:
        with pg_engine.connect() as conn:
            rows = dict(conn.execute(select(SystemFlag.name, SystemFlag.value)).all())
            flags = read_flags(conn)
        assert rows == SEED
        assert not flags.trading_enabled
        assert not flags.entries_allowed

    def test_enabled_without_halts_allows_entries(self, pg_engine: Engine) -> None:
        with pg_engine.begin() as conn:
            conn.execute(
                update(SystemFlag).where(SystemFlag.name == TRADING_ENABLED).values(value=True)
            )
            assert read_flags(conn).entries_allowed
            conn.execute(
                update(SystemFlag).where(SystemFlag.name == "halt_weekly_loss").values(value=True)
            )
            assert not read_flags(conn).entries_allowed

    def test_missing_rows_fail_closed(self, pg_engine: Engine) -> None:
        with pg_engine.begin() as conn:
            conn.execute(delete(SystemFlag))
            flags = read_flags(conn)
        assert not flags.trading_enabled
        assert flags.halt_daily_loss
        assert flags.halt_weekly_loss
        assert flags.halt_drawdown


def add_ticker(conn: Any, symbol: str = "ANET", **kwargs: Any) -> int:
    return int(
        conn.execute(
            insert(Ticker).values(symbol=symbol, **kwargs).returning(Ticker.id)
        ).scalar_one()
    )


def bar(ticker_id: int, **overrides: Any) -> dict[str, Any]:
    values = {
        "ticker_id": ticker_id,
        "session_date": date(2026, 10, 7),
        "open": 100.0,
        "high": 105.5,
        "low": 99.25,
        "close": 104.0,
        "raw_close": 416.0,
        "volume": 3_000_000_000,
    }
    return values | overrides


Rejects = Callable[[Callable[[Any], object]], None]


@pytest.fixture
def rejects(pg_engine: Engine) -> Rejects:
    def check(write: Callable[[Any], object]) -> None:
        with pytest.raises(IntegrityError), pg_engine.begin() as conn:
            write(conn)

    return check


class TestConstraints:
    def test_bar_round_trip_in_compact_types(self, pg_engine: Engine) -> None:
        with pg_engine.begin() as conn:
            tid = add_ticker(conn)
            conn.execute(insert(DailyBar).values(**bar(tid, close=1234.56, high=1300.0)))
            row = conn.execute(select(DailyBar)).mappings().one()
        assert row["close"] == pytest.approx(1234.56, abs=1e-3)
        assert row["volume"] == 3_000_000_000  # above int4

    @pytest.mark.parametrize(
        "overrides",
        [
            {"high": 98.0},  # high below the low
            {"low": 101.0},  # low above the open
            {"close": 106.0},  # close above the high
            {"open": 0.0, "low": 0.0},
            {"raw_close": -1.0},
            {"volume": 0},  # zero-volume bars are dropped at ingest (#111)
        ],
    )
    def test_bad_bars_are_rejected(
        self, pg_engine: Engine, rejects: Rejects, overrides: dict[str, Any]
    ) -> None:
        with pg_engine.begin() as conn:
            tid = add_ticker(conn)
        rejects(lambda conn: conn.execute(insert(DailyBar).values(**bar(tid, **overrides))))

    def test_duplicate_bar_is_rejected(self, pg_engine: Engine, rejects: Rejects) -> None:
        with pg_engine.begin() as conn:
            tid = add_ticker(conn)
            conn.execute(insert(DailyBar).values(**bar(tid)))
        rejects(lambda conn: conn.execute(insert(DailyBar).values(**bar(tid))))

    def test_bar_needs_a_ticker(self, rejects: Rejects) -> None:
        rejects(lambda conn: conn.execute(insert(DailyBar).values(**bar(999_999))))

    def test_one_active_security_per_symbol(self, pg_engine: Engine, rejects: Rejects) -> None:
        with pg_engine.begin() as conn:
            add_ticker(conn, "ABC", active=False, delisted_at=date(2019, 5, 1))
            add_ticker(conn, "ABC")  # a reused ticker: a new security
        rejects(lambda conn: add_ticker(conn, "ABC"))

    def test_asset_id_is_unique(self, pg_engine: Engine, rejects: Rejects) -> None:
        asset = "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415"
        with pg_engine.begin() as conn:
            add_ticker(conn, "ABC", alpaca_asset_id=asset, active=False)
        rejects(lambda conn: add_ticker(conn, "XYZ", alpaca_asset_id=asset))

    def test_session_checks(self, rejects: Rejects) -> None:
        open_at = datetime(2026, 10, 7, 13, 30, tzinfo=UTC)
        base = {
            "session_date": date(2026, 10, 7),
            "open_at": open_at,
            "close_at": datetime(2026, 10, 7, 20, 0, tzinfo=UTC),
            "is_early_close": False,
            "minutes": 390,
        }
        rejects(lambda c: c.execute(insert(MarketSession).values(**base | {"minutes": 391})))
        rejects(lambda c: c.execute(insert(MarketSession).values(**base | {"close_at": open_at})))
        rejects(lambda c: c.execute(insert(MarketSession).values(**base | {"data_status": "OK"})))

    def test_corporate_action_checks(self, rejects: Rejects) -> None:
        row = {
            "symbol": "NVDA",
            "action_type": "SPLIT",
            "ex_date": date(2026, 6, 10),
            "ratio": 10.0,
            "source": "alpaca",
            "source_id": "ca-1",
        }
        rejects(lambda c: c.execute(insert(CorporateAction).values(**row | {"action_type": "DIV"})))
        rejects(lambda c: c.execute(insert(CorporateAction).values(**row | {"handled": True})))
        rejects(lambda c: c.execute(insert(CorporateAction).values(**row | {"ratio": 0.0})))

    def test_resolving_a_mismatch_needs_a_note(self, pg_engine: Engine, rejects: Rejects) -> None:
        now = datetime(2026, 10, 7, 14, 0, tzinfo=UTC)
        rejects(
            lambda c: c.execute(insert(Mismatch).values(kind="RECONCILIATION", resolved_at=now))
        )
        with pg_engine.begin() as conn:
            conn.execute(
                insert(Mismatch).values(
                    kind="RECONCILIATION", symbol=None, resolved_at=now, resolution_note="fixed"
                )
            )

    def test_bars_use_a_brin_date_index(self, pg_engine: Engine) -> None:
        with pg_engine.connect() as conn:
            method = conn.execute(
                text(
                    "SELECT am.amname FROM pg_class c JOIN pg_am am ON am.oid = c.relam "
                    "WHERE c.relname = 'ix_daily_bars_session_date'"
                )
            ).scalar_one()
        assert method == "brin"


def test_alembic_ini_has_no_url() -> None:
    ini = Path(REPO_ROOT / "alembic.ini").read_text(encoding="utf-8")
    assert not re.search(r"^\s*sqlalchemy\.url\s*=", ini, flags=re.MULTILINE)


def base_row(ticker_id: int, **overrides: Any) -> dict[str, Any]:
    values = {
        "ticker_id": ticker_id,
        "start_date": date(2026, 6, 1),
        "end_date": date(2026, 7, 15),
        "status": "BROKEN_OUT",
        "sessions": 31,
        "high": 120.0,
        "low": 100.0,
        "low_date": date(2026, 6, 20),
        "depth": 1 / 6,
        "pivot": 118.5,
        "contraction_count": 2,
        "contractions": [
            {"high_date": "2026-06-01", "high": 120.0, "low_date": "2026-06-20", "low": 100.0},
            {"high_date": "2026-07-01", "high": 118.5, "low_date": "2026-07-08", "low": 112.0},
        ],
        "stage_number": 2,
        "count_reliable": True,
        "advance": 0.24,
    }
    return values | overrides


class TestBases:
    def test_round_trip(self, pg_engine: Engine) -> None:
        with pg_engine.begin() as conn:
            tid = add_ticker(conn)
            conn.execute(insert(DetectedBase).values(**base_row(tid)))
            row = conn.execute(select(DetectedBase)).mappings().one()
        assert row["stage_number"] == 2
        assert row["contractions"][1]["low"] == 112.0
        assert row["reset"] is False

    def test_a_forming_base_and_a_failed_candidate(self, pg_engine: Engine) -> None:
        with pg_engine.begin() as conn:
            tid = add_ticker(conn)
            conn.execute(
                insert(DetectedBase).values(
                    **base_row(tid, status="FAILED", stage_number=None, depth=0.55, low=54.0)
                )
            )
            conn.execute(
                insert(DetectedBase).values(
                    **base_row(tid, start_date=date(2026, 8, 3), end_date=None, status="FORMING")
                )
            )

    @pytest.mark.parametrize(
        "overrides",
        [
            {"status": "DONE"},
            {"end_date": None},  # only a forming base has no end
            {"status": "FORMING"},  # ... and a forming base has none
            {"stage_number": None},  # only a failed candidate has no stage
            {"status": "FAILED"},  # ... and a failed candidate has none
            {"stage_number": 0},
            {"end_date": date(2026, 6, 1)},
            {"low": 0.0},
            {"high": 99.0},
            {"depth": 1.0},
            {"contraction_count": -1},
        ],
    )
    def test_bad_rows_are_rejected(
        self, pg_engine: Engine, rejects: Rejects, overrides: dict[str, Any]
    ) -> None:
        with pg_engine.begin() as conn:
            tid = add_ticker(conn)
        rejects(lambda c: c.execute(insert(DetectedBase).values(**base_row(tid, **overrides))))

    def test_one_base_per_ticker_and_start(self, pg_engine: Engine, rejects: Rejects) -> None:
        with pg_engine.begin() as conn:
            tid = add_ticker(conn)
            conn.execute(insert(DetectedBase).values(**base_row(tid)))
        rejects(lambda c: c.execute(insert(DetectedBase).values(**base_row(tid))))

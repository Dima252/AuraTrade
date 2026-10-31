"""scripts/reset_trading_state.py: the refusals, and against the test Postgres, that trading
state goes, market data stays and the kill switch comes back on."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, insert, select, update

from auratrade.db.flags import SEED, TRADING_ENABLED
from auratrade.db.models import JobRunRow, MarketSession, Mismatch, SystemFlag, Ticker
from scripts import reset_trading_state as rts

NEON = "postgresql+psycopg://u:p@ep-paper.neon.tech/neondb"
DEV = "postgresql+psycopg://u:p@ep-dev.neon.tech/neondb"


def test_every_trading_table_exists_and_children_come_first() -> None:
    names = [t.name for t in rts.trading_tables()]
    assert set(names) == rts.TRADING_TABLES


@pytest.mark.parametrize(
    ("url", "dev", "local", "message"),
    [
        (DEV, DEV, False, "dev database"),
        ("postgresql://x:y@ep-dev.neon.tech/neondb", DEV, False, "dev database"),
        ("postgresql://a:b@localhost:5434/t", None, False, "localhost"),
    ],
)
def test_refusals(url: str, dev: str | None, local: bool, message: str) -> None:
    with pytest.raises(rts.RefusedError, match=message):
        rts.check_target(url, dev, allow_local=local)


def test_other_targets_pass() -> None:
    rts.check_target(NEON, DEV, allow_local=False)
    rts.check_target(NEON, None, allow_local=False)
    rts.check_target("postgresql://a:b@localhost:5434/t", DEV, allow_local=True)


def fill(engine: Engine) -> None:
    now = datetime(2026, 10, 22, 14, tzinfo=UTC)
    with engine.begin() as conn:
        conn.execute(insert(Ticker).values(symbol="SPY"))
        conn.execute(
            insert(MarketSession).values(
                session_date=date(2026, 10, 22),
                open_at=now,
                close_at=now + timedelta(hours=6, minutes=30),
                is_early_close=False,
                minutes=390,
                data_status="COMPLETE",
                calendar_check="AGREE",
            )
        )
        conn.execute(
            insert(JobRunRow).values(
                run_id="r1",
                job="ingest",
                session_date=date(2026, 10, 22),
                status="SUCCEEDED",
                started_at=now,
                finished_at=now,
                trading_mode="paper",
                risk_hash="0" * 64,
            )
        )
        enable = update(SystemFlag).where(SystemFlag.name == TRADING_ENABLED).values(value=True)
        conn.execute(enable)


def test_main_plans_then_clears(
    pg_engine: Engine,
    migrated_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fill(pg_engine)
    env = tmp_path / ".env.paper"
    env.write_text(f"DATABASE_URL={migrated_url}\n", encoding="utf-8")
    argv = ["--env-file", str(env), "--dev-env-file", str(tmp_path / "none"), "--allow-local"]

    assert rts.main(argv) == 0
    out = capsys.readouterr().out
    assert "would clear job_runs: 1 rows" in out
    assert migrated_url not in out
    with pg_engine.connect() as conn:
        assert conn.execute(select(JobRunRow.run_id)).scalars().all() == ["r1"]

    assert rts.main([*argv, "--apply"]) == 0
    assert "kill switch on" in capsys.readouterr().out
    with pg_engine.connect() as conn:
        assert conn.execute(select(JobRunRow)).first() is None
        assert conn.execute(select(Mismatch)).first() is None
        flags = dict(conn.execute(select(SystemFlag.name, SystemFlag.value)).all())
        assert flags[TRADING_ENABLED] is False
        assert flags == SEED
        assert conn.execute(select(Ticker.symbol)).scalars().all() == ["SPY"]
        assert conn.execute(select(MarketSession)).first() is not None


def test_main_refuses_without_touching(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = tmp_path / ".env.paper"
    env.write_text(f"DATABASE_URL={DEV}\n", encoding="utf-8")
    dev = tmp_path / ".env"
    dev.write_text(f"DATABASE_URL={DEV}\n", encoding="utf-8")
    assert rts.main(["--env-file", str(env), "--dev-env-file", str(dev), "--apply"]) == 1
    assert "refused: the target is the dev database" in capsys.readouterr().err


def test_main_needs_a_url(tmp_path: Path) -> None:
    env = tmp_path / ".env.paper"
    env.write_text("OTHER=1\n", encoding="utf-8")
    assert rts.main(["--env-file", str(env)]) == 2
    assert rts.main(["--env-file", str(tmp_path / "missing")]) == 2

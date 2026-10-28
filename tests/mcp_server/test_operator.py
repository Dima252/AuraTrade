from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, insert, update

from auratrade.config import RiskConfig, load_risk_config
from auratrade.db.flags import HALT_DRAWDOWN, TRADING_ENABLED
from auratrade.db.models import (
    AccountSnapshot,
    FundStop,
    JobRunRow,
    LlmDecision,
    MarketSession,
    Mismatch,
    PortfolioJournal,
    PortfolioTarget,
    PromptVersion,
    SystemFlag,
    TaxLot,
)
from auratrade.execution.broker import (
    Account,
    BrokerOrder,
    BrokerPosition,
    OrderClass,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)
from auratrade.mcp_server import operator
from auratrade.mcp_server.sources import read_only_engine

TODAY = date(2026, 10, 28)
E = date(2026, 11, 2)


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def _sessions(first: date, last: date) -> list[dict[str, Any]]:
    out, day = [], first
    while day <= last:
        if day.weekday() < 5:
            out.append(
                {
                    "session_date": day,
                    "open_at": _at(day, 13, 30),
                    "close_at": _at(day, 20),
                    "is_early_close": False,
                    "minutes": 390,
                }
            )
        day += timedelta(days=1)
    return out


def _run(job: str, day: date, status: str, hour: int, error: str | None = None) -> dict[str, Any]:
    return {
        "run_id": f"{job}{hour}",
        "job": job,
        "session_date": day,
        "status": status,
        "started_at": _at(day, hour),
        "finished_at": _at(day, hour, 1),
        "trading_mode": "paper",
        "risk_hash": "abc",
        "details": {"n": hour},
        "error": error,
    }


FACTS = {
    "trend": {"state": "UP", "month_end_value": 110.0, "sma": 100.0, "capped": False},
    "account": {"equity": 100_000.0, "lots": {"SSO": []}, "prices": {"SSO": 50.0}},
    "macro": {"VIX": 17.5},
    "regime": {"regime": "CORRECTION"},
    "event": None,
    "notes": [],
    "etfs": [],
}


def _seed(conn: Connection) -> None:
    conn.execute(insert(MarketSession).values(_sessions(date(2026, 10, 1), date(2026, 12, 31))))
    conn.execute(
        insert(JobRunRow),
        [
            _run("ingest", date(2026, 10, 27), "FAILED", 20, error="x" * 3000),
            _run("ingest", TODAY, "SUCCEEDED", 21),
            _run("allocate", TODAY, "SKIPPED_ALREADY_RAN", 22),
        ],
    )
    conn.execute(update(SystemFlag).where(SystemFlag.name == TRADING_ENABLED).values(value=True))
    conn.execute(
        update(SystemFlag)
        .where(SystemFlag.name == HALT_DRAWDOWN)
        .values(details={"peak": 100_000.0})
    )
    conn.execute(
        insert(Mismatch),
        [
            {
                "kind": "RECONCILIATION",
                "symbol": "SLN",
                "details": {"qty": 7},
                "resolved_at": None,
                "resolution_note": None,
            },
            {
                "kind": "CORPORATE_ACTION",
                "symbol": "SSO",
                "details": {},
                "resolved_at": _at(TODAY, 12),
                "resolution_note": "fixed",
            },
        ],
    )
    conn.execute(
        insert(AccountSnapshot).values(
            session_date=TODAY,
            equity=100_000.0,
            cash=1_000.0,
            long_market_value=99_000.0,
            peak_equity=100_000.0,
            drawdown=0.0,
            open_positions=2,
            holdings={"SSO": {"units": 100, "value": 5_000.0}},
            tax_owed=250.0,
        )
    )
    conn.execute(insert(PromptVersion).values(id="pm-2.abc", version="pm-2", text="prompt"))
    llm_id = conn.execute(
        insert(LlmDecision)
        .values(
            session_date=E,
            role="portfolio_manager",
            prompt_id="pm-2.abc",
            requested_model="claude-opus-5-5",
            model="claude-opus-5-5",
            effort="high",
            answer={"core_shift": 0.0, "satellite": None},
            turns=[{"t": 1}, {"t": 2}],
            cost_usd=0.21,
            cost_alert=False,
            fallback=False,
        )
        .returning(LlmDecision.id)
    ).scalar_one()
    common = {
        "kind": "REBALANCE",
        "ranking_session": date(2026, 10, 30),
        "trend": "UP",
        "capped": False,
        "halted": False,
        "rule_level": 1.3,
        "twin_targets": {"SPY": 0.85, "SSO": 0.45},
        "facts": FACTS,
        "risk_hash": "abc",
    }
    targets_id = conn.execute(
        insert(PortfolioTarget)
        .values(
            **common,
            execution_session=E,
            status="DECIDED",
            llm_decision_id=llm_id,
            level=1.3,
            core_level=1.375,
            satellite={"SPY": 0.2},
            targets={"SPY": 0.85, "SSO": 0.45},
            satellite_tax=12.5,
            findings=[{"rule": "hold", "outcome": "kept"}],
        )
        .returning(PortfolioTarget.id)
    ).scalar_one()
    conn.execute(
        insert(PortfolioTarget).values(
            **common, execution_session=date(2025, 12, 1), status="DECIDED", satellite_tax=99.0
        )
    )
    conn.execute(
        insert(PortfolioJournal).values(
            execution_session=E, targets_id=targets_id, summary="Kept the neutral", decision={}
        )
    )
    open_lot = {"closed": None, "proceeds": None, "realized_gain": None}
    conn.execute(
        insert(TaxLot),
        [
            {"kind": "LOT", "symbol": "SSO", "opened": E, "units": 60.0, "cost": 3_000.0}
            | open_lot,
            {"kind": "LOT", "symbol": "SSO", "opened": E, "units": 40.0, "cost": 2_000.0}
            | open_lot,
            {
                "kind": "LOT",
                "symbol": "GLD",
                "opened": E,
                "units": 10.0,
                "cost": 1_000.0,
                "closed": date(2026, 12, 1),
                "proceeds": 1_100.0,
                "realized_gain": 100.0,
            },
            {
                "kind": "INCOME",
                "symbol": "BIL",
                "opened": date(2026, 12, 1),
                "units": 0,
                "cost": 30.0,
            }
            | open_lot,
            {
                "kind": "INCOME",
                "symbol": "BIL",
                "opened": date(2025, 12, 1),
                "units": 0,
                "cost": 9.0,
            }
            | open_lot,
        ],
    )
    conn.execute(insert(FundStop).values(symbol="SSO", session_date=E, level=45.0, change="ENTRY"))


@pytest.fixture
def engine(pg_engine: Engine, migrated_url: str) -> Iterator[Engine]:
    with pg_engine.begin() as conn:
        _seed(conn)
    read_only = read_only_engine(migrated_url)
    yield read_only
    read_only.dispose()


@pytest.fixture(scope="module")
def risk() -> RiskConfig:
    return load_risk_config()


def test_status(engine: Engine) -> None:
    with engine.connect() as conn:
        out = operator.status(conn, today=TODAY, environment="paper")

    assert out["kill_switch"] == "off (increases allowed)"
    assert out["drawdown_halt"] == {"on": False, "peak": 100_000.0, "since": None}
    assert [m["symbol"] for m in out["open_mismatches"]] == ["SLN"]
    assert out["next_rebalance"] == {
        "ranking_session": "2026-10-30",
        "execution_session": "2026-11-02",
        "today_is": None,
    }
    assert {r["job"]: r["status"] for r in out["last_runs"]} == {
        "allocate": "SKIPPED_ALREADY_RAN",
        "ingest": "SUCCEEDED",
    }
    assert "details" not in out["last_runs"][0]


def test_today_is_r_or_e(engine: Engine) -> None:
    with engine.connect() as conn:
        on_r = operator.next_rebalance(conn, date(2026, 10, 30))
        on_e = operator.next_rebalance(conn, E)
        past = operator.next_rebalance(conn, date(2027, 6, 1))

    assert on_r is not None
    assert on_r["today_is"] == "R"
    assert on_e is not None
    assert on_e["today_is"] == "E"
    assert past is None


def test_kill_switch_on_reads_as_blocked(engine: Engine, pg_engine: Engine) -> None:
    with pg_engine.begin() as conn:
        conn.execute(
            update(SystemFlag).where(SystemFlag.name == TRADING_ENABLED).values(value=False)
        )
    with engine.connect() as conn:
        out = operator.status(conn, today=TODAY, environment="paper")

    assert out["kill_switch"] == "ON (increases blocked)"


def test_jobs_filtered_with_details(engine: Engine) -> None:
    with engine.connect() as conn:
        failed = operator.jobs(conn, job="ingest", status="failed", limit=5)["runs"]
        latest = operator.jobs(conn, job=None, status=None, limit=0)["runs"]

    [run] = failed
    assert run["session"] == "2026-10-27"
    assert len(run["error"]) == operator.ERROR_CHARS
    assert run["details"] == {"n": 20}
    assert [r["job"] for r in latest] == ["allocate"]


@dataclass
class Reader:
    positions_: list[BrokerPosition] = field(default_factory=list)
    orders: list[BrokerOrder] = field(default_factory=list)

    def account(self) -> Account:
        return Account(equity=100_500.0, cash=500.0, long_market_value=100_000.0)

    def is_market_open(self) -> bool:
        return False

    def positions(self) -> list[BrokerPosition]:
        return self.positions_

    def open_orders(self) -> list[BrokerOrder]:
        return self.orders


def _order(
    oid: str, symbol: str, side: Side, kind: OrderType, qty: int, **prices: float
) -> BrokerOrder:
    return BrokerOrder(
        id=oid,
        client_order_id=f"c-{oid}",
        symbol=symbol,
        side=side,
        type=kind,
        time_in_force=TimeInForce.GTC,
        order_class=OrderClass.SIMPLE,
        qty=qty,
        filled_qty=0,
        status=OrderStatus.NEW,
        **prices,
    )


def test_portfolio_checks_the_stop_against_the_plan(engine: Engine, risk: RiskConfig) -> None:
    reader = Reader(
        [BrokerPosition("SSO", 100, 50.0, 52.0), BrokerPosition("SPY", 10, 600.0, None)],
        [
            _order("1", "SSO", Side.SELL, OrderType.STOP, 60, stop_price=46.0),
            _order("2", "SSO", Side.SELL, OrderType.STOP, 40, stop_price=44.0),
            _order("3", "GLD", Side.BUY, OrderType.LIMIT, 5, limit_price=300.0),
        ],
    )
    with engine.connect() as conn:
        out = operator.portfolio(conn, risk, reader)

    broker = out["broker"]
    assert broker["equity"] == 100_500.0
    assert broker["positions"][0] == {
        "symbol": "SPY",
        "qty": 10,
        "avg_entry_price": 600.0,
        "price": None,
        "value": None,
    }
    assert broker["positions"][1]["value"] == 5_200.0
    [sso] = broker["stops"]
    assert sso["plan_level"] == 45.0
    assert sso["covered"] == 60  # the 44.00 stop is below the plan's 45.00
    assert not sso["protected"]
    assert sso["problems"] == ["BELOW_PLAN", "UNCOVERED"]
    assert [o["symbol"] for o in broker["other_open_orders"]] == ["GLD"]
    assert "id" not in broker["other_open_orders"][0]
    assert out["last_close"]["equity_after_tax"] == 99_750.0


def test_portfolio_without_a_broker(engine: Engine, risk: RiskConfig) -> None:
    with engine.connect() as conn:
        out = operator.portfolio(conn, risk, None)

    assert out["broker"] is None
    assert out["last_close"]["holdings"] == {"SSO": {"units": 100, "value": 5_000.0}}


def test_trend_is_the_latest_reading_without_the_heavy_facts(engine: Engine) -> None:
    with engine.connect() as conn:
        out = operator.trend(conn)

    assert out["latest"]["execution_session"] == "2026-11-02"
    assert out["latest"]["rule_level"] == 1.3
    assert out["facts"]["trend"]["state"] == "UP"
    assert out["facts"]["account"] == {"equity": 100_000.0}
    assert out["facts"]["macro"] == {"VIX": 17.5}


def test_tax(engine: Engine, risk: RiskConfig) -> None:
    with engine.connect() as conn:
        out = operator.tax(conn, risk, 2026)

    assert out["open_lots"]["SSO"]["units"] == 100.0
    assert out["open_lots"]["SSO"]["cost"] == 5_000.0
    assert "GLD" not in out["open_lots"]
    assert out["realized_gain_by_year"] == {"2026": 100.0}
    assert out["income_this_year"] == {"BIL": 30.0}
    assert out["tax_owed_if_sold_at_last_close"] == 250.0
    budget = out["satellite_tax_budget"]
    assert budget["budget"] == pytest.approx(risk.portfolio.satellite_tax_budget * 100_000.0)
    assert budget["used"] == 12.5
    assert budget["left"] == pytest.approx(budget["budget"] - 12.5)


def test_decision_whole(engine: Engine) -> None:
    with engine.connect() as conn:
        latest = operator.decision(conn, None)
        missing = operator.decision(conn, date(2026, 6, 1))
        earlier = operator.decision(conn, date(2025, 12, 1))

    assert latest["decision"]["execution_session"] == "2026-11-02"
    assert latest["decision"]["findings"] == [{"rule": "hold", "outcome": "kept"}]
    assert latest["llm"]["cost_usd"] == 0.21
    assert latest["llm"]["turns"] == 2
    assert latest["journal"]["summary"] == "Kept the neutral"
    assert latest["facts"]["macro"] == {"VIX": 17.5}
    assert missing["decision"] is None
    assert earlier["llm"] is None
    assert earlier["journal"] is None


def test_an_empty_database(pg_engine: Engine, migrated_url: str, risk: RiskConfig) -> None:
    engine = read_only_engine(migrated_url)
    try:
        with engine.connect() as conn:
            assert operator.trend(conn)["latest"] is None
            assert operator.next_rebalance(conn, TODAY) is None
            assert operator.portfolio(conn, risk, None)["last_close"] is None
            budget = operator.tax(conn, risk, 2026)["satellite_tax_budget"]
            assert budget["budget"] is None
            assert budget["left"] is None
            decided = operator.decision(conn, None)
            assert decided["decision"] is None
    finally:
        engine.dispose()


def test_plain() -> None:
    assert operator.plain({"a": float("nan"), 1: (date(2026, 1, 2), {3})}) == {
        "a": None,
        "1": ["2026-01-02", [3]],
    }

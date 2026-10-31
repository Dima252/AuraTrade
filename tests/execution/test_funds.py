"""The portfolio's funds in the order layer (`execution.funds`): which carry a stop,
the leveraged fund's tighten-only level, and splits."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import insert, select

from auratrade.db.models import CorporateAction, Mismatch
from auratrade.execution import funds
from auratrade.execution.broker import BrokerPosition
from auratrade.execution.store import entities
from tests.execution.conftest import World

DAY = date(2026, 10, 22)
NOW = datetime(2026, 10, 22, 13, 20, tzinfo=UTC)


def test_only_the_leveraged_fund_carries_a_stop(world: World) -> None:
    cfg = world.risk.portfolio
    assert "SSO" in funds.funds(cfg)
    assert "SSO" not in funds.unstopped(cfg)
    assert {"SPY", "BIL", "QQQ"} <= funds.unstopped(cfg)


def test_the_level_is_tighten_only_but_set_from_flat_and_by_a_split(world: World) -> None:
    with world.engine.begin() as conn:
        assert funds.stop_level(conn, "SSO") is None
        assert funds.write_stop_level(conn, "SSO", 70.0, session=DAY, change="ENTRY") == 70.0
        assert funds.write_stop_level(conn, "SSO", 65.0, session=DAY, change="RAISE") == 70.0
        assert funds.write_stop_level(conn, "SSO", 84.0, session=DAY, change="RAISE") == 84.0
        assert funds.write_stop_level(conn, "SSO", 42.0, session=DAY, change="SPLIT") == 42.0
        assert funds.write_stop_level(conn, "SSO", 30.0, session=DAY, change="ENTRY") == 30.0
        found = funds.stop_level(conn, "SSO")
        assert found is not None
        assert (found.level, found.change) == (30.0, "ENTRY")
        assert funds.levels(conn, world.risk.portfolio, ["SPY"]) == {}
        assert funds.levels(conn, world.risk.portfolio, ["SSO"]) == {"SSO": 30.0}
        assert funds.levels(conn, world.risk.portfolio, []) == {}


def split(world: World, symbol: str, ratio: float | None, ex: date = DAY) -> None:
    with world.engine.begin() as conn:
        conn.execute(
            insert(CorporateAction).values(
                symbol=symbol,
                action_type="SPLIT",
                ex_date=ex,
                ratio=ratio,
                source="test",
                source_id=f"{symbol}-{ex}-{ratio}",
            )
        )


def held(*symbols: str) -> dict[str, BrokerPosition]:
    return {s: BrokerPosition(s, 10, 100.0) for s in symbols}


def test_a_split_of_an_unstopped_fund_opens_a_mismatch_and_blocks_it(world: World) -> None:
    cfg = world.risk.portfolio
    split(world, "QQQ", 4.0)
    split(world, "SPY", None)  # no ratio yet: left for a later run
    with world.engine.begin() as conn:
        alerts = funds.apply_splits(conn, cfg, held("QQQ", "SPY"), session=DAY, now=NOW)
        assert funds.mismatched(conn, cfg) == frozenset({"QQQ"})
        open_actions = entities(conn, select(CorporateAction).where(~CorporateAction.handled))
    assert len(alerts) == 1
    assert "split 4:1 in QQQ" in alerts[0]
    assert "40 expected" in alerts[0]
    assert [a.symbol for a in open_actions] == ["SPY"]


def test_a_split_before_the_level_was_set_doesnt_scale_it(world: World) -> None:
    cfg = world.risk.portfolio
    split(world, "SSO", 2.0, ex=date(2026, 10, 20))
    with world.engine.begin() as conn:
        funds.write_stop_level(conn, "SSO", 70.0, session=DAY, change="ENTRY")
        alerts = funds.apply_splits(conn, cfg, held("SSO"), session=DAY, now=NOW)
        level = funds.stop_level(conn, "SSO")
        mismatches = entities(conn, select(Mismatch))
    assert level is not None
    assert level.level == 70.0  # bought after the ex-date: already at the new shares' price
    assert "scaled" not in alerts[0]
    assert [m.symbol for m in mismatches] == ["SSO"]


def test_nothing_held_nothing_to_split(world: World) -> None:
    with world.engine.begin() as conn:
        assert funds.apply_splits(conn, world.risk.portfolio, {}, session=DAY, now=NOW) == []

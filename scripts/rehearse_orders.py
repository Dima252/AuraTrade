"""Rehearse the portfolio's orders on the `dev` paper account.

    uv run python scripts/rehearse_orders.py plan --level 1.3 --satellite SPY=0.15,GLD=0.05
    TRADING_MODE=paper uv run python -m auratrade.jobs.execute_close --session <today>
    TRADING_MODE=paper uv run python -m auratrade.jobs.stop_guard --session <today>
    uv run python scripts/rehearse_orders.py plan --level 0.5 --satellite SPY=0.15,GLD=0.05
    TRADING_MODE=paper uv run python -m auratrade.jobs.execute_close --session <today>
    TRADING_MODE=paper uv run auratrade flatten --confirm
    uv run python scripts/rehearse_orders.py clear

`plan` writes a decided rebalance for today, as `portfolio_manager` would on E, so the real
jobs trade it: the runner, the order lock, the stop invariant, the kill switch, the sell
sequence and the records, against the real broker. Its targets come from
`core_allocation.book_targets` for the level and satellite given, with no Claude call. A second
`plan` the same day replaces the row (one rebalance per E), so the next run trades toward the
new targets. `clear` deletes the rehearsal's rows (marked in their facts), and with them the
journal rows `simulate` made from them; the orders, fills and stop levels stay, as the broker's
record does.

Refused anywhere but `ENVIRONMENT=dev`: the `paper` account is the trial's.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, delete, insert, select

from auratrade.calendar.sessions import load_calendar
from auratrade.config import PortfolioConfig, RiskConfig, Settings, load_risk_config
from auratrade.db.models import PortfolioJournal, PortfolioTarget
from auratrade.execution.store import entities
from auratrade.strategies import core_allocation

ENVIRONMENT = "dev"
MARK = "rehearsal"
NY = ZoneInfo("America/New_York")


class RehearsalError(RuntimeError):
    pass


def parse_satellite(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for part in filter(None, (p.strip() for p in text.split(","))):
        symbol, _, weight = part.partition("=")
        out[symbol.strip().upper()] = float(weight)
    return out


def targets(level: float, satellite: Mapping[str, float], cfg: PortfolioConfig) -> dict[str, float]:
    """The book's targets, after the checks a decision's satellite would have passed."""
    unknown = sorted(set(satellite) - set(cfg.satellite_list))
    if unknown:
        raise RehearsalError(f"not on the approved list: {unknown}")
    if abs(sum(satellite.values()) - cfg.satellite_share) > 1e-9:
        raise RehearsalError(f"the satellite must sum to {cfg.satellite_share}")
    over = [s for s, w in satellite.items() if s != cfg.market and w > cfg.max_etf_weight]
    if over:
        raise RehearsalError(f"above {cfg.max_etf_weight} of equity: {over}")
    if not 0 < level <= cfg.max_level:
        raise RehearsalError(f"level {level} outside (0, {cfg.max_level}]")
    return core_allocation.book_targets(level, satellite, cfg)


def _is_rehearsal(row: PortfolioTarget) -> bool:
    return bool((row.facts or {}).get(MARK))


def _delete(engine: Engine, ids: Sequence[int]) -> None:
    with engine.begin() as conn:
        conn.execute(delete(PortfolioJournal).where(PortfolioJournal.targets_id.in_(ids)))
        conn.execute(delete(PortfolioTarget).where(PortfolioTarget.id.in_(ids)))


def plan(
    engine: Engine,
    risk: RiskConfig,
    today: date,
    level: float,
    satellite: Mapping[str, float],
    *,
    now: datetime,
) -> dict[str, float]:
    cfg = risk.portfolio
    book = targets(level, satellite, cfg)
    with engine.connect() as conn:
        calendar = load_calendar(conn)
        existing = entities(
            conn,
            select(PortfolioTarget).where(
                PortfolioTarget.kind == "REBALANCE", PortfolioTarget.execution_session == today
            ),
        )
    if not calendar.is_session(today):
        raise RehearsalError(f"{today} is not a session")
    if any(not _is_rehearsal(row) for row in existing):
        raise RehearsalError(f"{today} has a real rebalance: nothing written")
    _delete(engine, [row.id for row in existing])
    trend = "UP" if level > 1 else "DOWN"
    with engine.begin() as conn:
        conn.execute(
            insert(PortfolioTarget).values(
                kind="REBALANCE",
                ranking_session=calendar.previous_session(today).day,
                execution_session=today,
                status="DECIDED",
                trend=trend,
                capped=False,
                halted=False,
                rule_level=level,
                twin_targets=core_allocation.twin_targets(level, cfg),
                facts={MARK: True},
                level=level,
                core_level=core_allocation.core_level(level, cfg),
                satellite=dict(satellite),
                targets=book,
                held_satellite=False,
                fell_back=False,
                satellite_tax=0.0,
                risk_hash=risk.config_hash,
                decided_at=now,
            )
        )
    return book


def clear(engine: Engine) -> int:
    with engine.connect() as conn:
        rows = entities(conn, select(PortfolioTarget))
    ids = [row.id for row in rows if _is_rehearsal(row)]
    _delete(engine, ids)
    return len(ids)


def main(
    argv: Sequence[str] | None = None,
    *,
    settings_factory: Callable[[], Settings] | None = None,
    engine_factory: Callable[[Settings], Engine] | None = None,
    out: Callable[[str], None] = print,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="write today's decided rebalance for the real jobs to trade")
    p.add_argument("--level", type=float, required=True, help="the book's S&P exposure")
    p.add_argument("--satellite", type=parse_satellite, required=True, help="SYM=W,SYM=W")
    sub.add_parser("clear", help="delete the rehearsal's rows")
    args = parser.parse_args(argv)

    from auratrade.db.engine import engine_from_settings
    from auratrade.jobs.wiring import load_runtime_settings

    settings = (settings_factory or load_runtime_settings)()
    if settings.environment != ENVIRONMENT:
        out(f"refused: ENVIRONMENT is {settings.environment!r}; rehearsals run on {ENVIRONMENT!r}")
        return 1
    engine = (engine_factory or engine_from_settings)(settings)
    try:
        if args.command == "clear":
            out(f"deleted {clear(engine)} rehearsal rows")
            return 0
        risk = load_risk_config(settings.risk_config_path)
        now = datetime.now(UTC)
        try:
            book = plan(
                engine, risk, now.astimezone(NY).date(), args.level, args.satellite, now=now
            )
        except RehearsalError as exc:
            out(f"refused: {exc}")
            return 1
        out("targets: " + ", ".join(f"{s} {w:.1%}" for s, w in sorted(book.items())))
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())

"""Operator commands (README §5.9).

    uv run auratrade kill on        # increases off; resting buy orders cancelled at once
    uv run auratrade kill off       # increases on (a fresh database starts with them off)
    uv run auratrade kill status
    uv run auratrade mismatches     # open reconciliation and corporate-action mismatches
    uv run auratrade resolve 12 --note "DXJ split fixed by hand at the broker"
    uv run auratrade halt status    # the drawdown halt and its peak (README §7.3)
    uv run auratrade halt reset     # off, the peak at the broker's equity (after a paper reset)
    uv run auratrade flatten --confirm   # kill switch on, then sell every position (market hours)

The kill switch blocks increases of exposure only; it never touches a protective stop, a sale
toward a lower target, the halt's sale or reconciliation (README §5.9). `kill on` cancels
resting buy orders under the order lock; every order job also checks the flag and cancels any
that remain. `flatten` is manual only: nothing automatic calls it. It turns the kill switch on
first, so nothing buys the positions back, and sells the leveraged fund with the sell sequence
(its stop restored if the sale fails), everything else with a marketable limit.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, insert, select, update

from auratrade.config import RiskConfig, Settings, TradingMode, load_risk_config
from auratrade.db.flags import HALT_DRAWDOWN, TRADING_ENABLED, read_flags
from auratrade.db.models import Mismatch, SystemFlag
from auratrade.execution import funds
from auratrade.execution import halt as drawdown_halt
from auratrade.execution.broker import Broker, PriceSource
from auratrade.execution.orders import OrderDesk
from auratrade.execution.rebalance import leveraged_stop
from auratrade.execution.runtime import Lock
from auratrade.execution.store import entities


@dataclass(frozen=True)
class CliDeps:
    settings: Settings
    risk: RiskConfig
    engine: Engine
    broker: Callable[[], Broker]
    lock: Callable[[], Lock]
    out: Callable[[str], None]
    prices: Callable[[], PriceSource]
    sleep: Callable[[float], None] = time.sleep


def production_deps() -> CliDeps:
    from auratrade.db.engine import engine_from_settings
    from auratrade.execution.alpaca_broker import AlpacaBroker
    from auratrade.execution.lock import OrderLock
    from auratrade.jobs.wiring import load_runtime_settings, make_alerter
    from auratrade.log import configure_logging

    settings = load_runtime_settings()
    configure_logging(settings.secret_values())
    risk = load_risk_config(settings.risk_config_path)

    def broker() -> Broker:
        if settings.alpaca_api_key is None or settings.alpaca_secret_key is None:
            raise SystemExit("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
        return AlpacaBroker.paper(
            settings.alpaca_api_key.get_secret_value(),
            settings.alpaca_secret_key.get_secret_value(),
        )

    def prices() -> PriceSource:
        from auratrade.data.alpaca_data import AlpacaDataClient, alpaca_http
        from auratrade.execution.alpaca_broker import AlpacaPrices

        if settings.alpaca_api_key is None or settings.alpaca_secret_key is None:
            raise SystemExit("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
        http = alpaca_http(
            settings.alpaca_api_key.get_secret_value(),
            settings.alpaca_secret_key.get_secret_value(),
        )
        return AlpacaPrices(AlpacaDataClient(http), risk.execution.decision_price)

    def lock() -> Lock:
        assert settings.database_url is not None  # noqa: S101 - engine_from_settings checked it
        return OrderLock(
            settings.database_url.get_secret_value(),
            risk.execution,
            alert=make_alerter(settings).alert,
        )

    return CliDeps(
        settings=settings,
        risk=risk,
        engine=engine_from_settings(settings),
        broker=broker,
        lock=lock,
        out=_print,
        prices=prices,
    )


def _print(text: str) -> None:
    sys.stdout.write(text + "\n")


def _set_trading(deps: CliDeps, enabled: bool) -> None:
    now = datetime.now(UTC)
    with deps.engine.begin() as conn:
        updated = conn.execute(
            update(SystemFlag)
            .where(SystemFlag.name == TRADING_ENABLED)
            .values(value=enabled, updated_at=now, updated_by="cli")
        ).rowcount
        if not updated:
            conn.execute(
                insert(SystemFlag).values(
                    name=TRADING_ENABLED, value=enabled, updated_at=now, updated_by="cli"
                )
            )


def kill(deps: CliDeps, action: str) -> int:
    if action == "status":
        with deps.engine.connect() as conn:
            flags = read_flags(conn)
        state = "off (increases allowed)" if flags.trading_enabled else "ON (increases blocked)"
        deps.out(f"kill switch {state}")
        return 0
    if action == "off":
        _set_trading(deps, True)
        deps.out("kill switch off: increases allowed (the halt and the checks still apply)")
        return 0
    _set_trading(deps, False)
    deps.out("kill switch ON: no increases of exposure")
    if deps.settings.trading_mode is not TradingMode.PAPER:
        deps.out("TRADING_MODE is not paper: no orders to cancel")
        return 0
    broker = deps.broker()
    with deps.lock().hold("cli_kill"):
        desk = OrderDesk(
            broker=broker,
            prices=_NoPrices(),
            cfg=deps.risk.execution,
            session=datetime.now(UTC).date(),
            market_open=broker.is_market_open(),
            sleep=lambda _: None,
            alert=deps.out,
        )
        count = desk.cancel_entries(why="kill switch")
    deps.out(f"cancelled {count} resting buy orders")
    return 0


class _NoPrices:
    def prices(self, symbols: object) -> dict[str, float | None]:
        return {}


def mismatches(deps: CliDeps) -> int:
    with deps.engine.connect() as conn:
        rows = entities(
            conn, select(Mismatch).where(Mismatch.resolved_at.is_(None)).order_by(Mismatch.id)
        )
        found = False
        for row in rows:
            found = True
            deps.out(
                f"{row.id}  {row.kind}  {row.symbol or '(account)'}  "
                f"{row.detected_at:%Y-%m-%d %H:%M}  {row.details}"
            )
    if not found:
        deps.out("no open mismatches")
    return 0


def resolve(deps: CliDeps, mismatch_id: int, note: str) -> int:
    with deps.engine.begin() as conn:
        updated = conn.execute(
            update(Mismatch)
            .where(Mismatch.id == mismatch_id, Mismatch.resolved_at.is_(None))
            .values(resolved_at=datetime.now(UTC), resolution_note=note)
        ).rowcount
    if not updated:
        deps.out(f"no open mismatch {mismatch_id}")
        return 1
    deps.out(f"mismatch {mismatch_id} resolved")
    return 0


def halt(deps: CliDeps, action: str) -> int:
    if action == "reset":
        equity = deps.broker().account().equity
        session = datetime.now(ZoneInfo("America/New_York")).date()
        with deps.engine.begin() as conn:
            drawdown_halt.reset(conn, session, equity, by="cli")
        deps.out(f"drawdown halt off; its peak reset to the equity {equity:,.2f}")
        return 0
    with deps.engine.connect() as conn:
        row = conn.execute(
            select(SystemFlag.value, SystemFlag.details).where(SystemFlag.name == HALT_DRAWDOWN)
        ).first()
    if row is None:
        deps.out("drawdown halt: no flag row (reads as ON)")
        return 0
    details = dict(row.details or {})
    peak = details.get("peak")
    state = "ON (no leverage)" if row.value else "off"
    text = f"drawdown halt {state}"
    text += "; no peak yet" if peak is None else f"; peak {float(peak):,.2f}"
    if row.value and details.get("since"):
        text += f", on since {details['since']}"
    deps.out(text)
    return 0


FLATTEN = "FLATTEN"


def flatten(deps: CliDeps, confirm: bool) -> int:
    if not confirm:
        deps.out("flatten sells every position at the broker: run it with --confirm")
        return 1
    _set_trading(deps, False)
    deps.out("kill switch ON: nothing is bought back until `auratrade kill off`")
    if deps.settings.trading_mode is not TradingMode.PAPER:
        deps.out("TRADING_MODE is not paper: no orders sent")
        return 0
    broker = deps.broker()
    lev = deps.risk.portfolio.leveraged
    with deps.lock().hold("cli_flatten"):
        desk = OrderDesk(
            broker=broker,
            prices=deps.prices(),
            cfg=deps.risk.execution,
            session=datetime.now(ZoneInfo("America/New_York")).date(),
            market_open=broker.is_market_open(),
            sleep=deps.sleep,
            alert=deps.out,
        )
        if not desk.market_open:
            deps.out("the market is closed: nothing sold; run flatten during market hours")
            return 1
        deps.out(f"cancelled {desk.cancel_entries(why='flatten')} resting buy orders")
        with deps.engine.connect() as conn:
            current = funds.stop_level(conn, lev)
        owed = 0
        for position in sorted(broker.positions(), key=lambda p: p.symbol):
            if position.symbol == lev:
                level = (
                    current.level
                    if current is not None
                    else leveraged_stop(desk.price(lev) or 0.0, None, deps.risk.portfolio)
                )
                result = desk.sell(lev, position.qty, FLATTEN, level)
            else:
                result = desk.sell_plain(position.symbol, position.qty, FLATTEN)
            deps.out(f"{position.symbol}: sold {result.sold} of {position.qty} ({result.status})")
            owed += result.owed
    if owed:
        deps.out(f"{owed} shares still held: run flatten again")
        return 1
    deps.out("flat")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auratrade", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    k = sub.add_parser("kill", help="the kill switch: increases of exposure only")
    k.add_argument("action", choices=("on", "off", "status"))
    sub.add_parser("mismatches", help="list open mismatches")
    r = sub.add_parser("resolve", help="mark a mismatch resolved after fixing it")
    r.add_argument("id", type=int)
    r.add_argument("--note", required=True)
    h = sub.add_parser("halt", help="the drawdown halt: its state, or reset it")
    h.add_argument("action", choices=("status", "reset"))
    f = sub.add_parser("flatten", help="kill switch on, then sell every position (manual only)")
    f.add_argument("--confirm", action="store_true", help="really sell everything")
    return parser


def main(
    argv: Sequence[str] | None = None, *, deps_factory: Callable[[], CliDeps] = production_deps
) -> int:
    args = build_parser().parse_args(argv)
    deps = deps_factory()
    try:
        match args.command:
            case "kill":
                return kill(deps, args.action)
            case "mismatches":
                return mismatches(deps)
            case "halt":
                return halt(deps, args.action)
            case "flatten":
                return flatten(deps, args.confirm)
            case _:
                return resolve(deps, args.id, args.note)
    finally:
        deps.engine.dispose()


if __name__ == "__main__":
    sys.exit(main())

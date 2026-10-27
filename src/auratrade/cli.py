"""Operator commands (README §5.13).

    uv run auratrade kill on        # entries off; resting entry orders cancelled at once
    uv run auratrade kill off       # entries on (a fresh database starts with them off)
    uv run auratrade kill status
    uv run auratrade mismatches     # open reconciliation and corporate-action mismatches
    uv run auratrade resolve 12 --note "DXJ split fixed by hand at the broker"
    uv run auratrade halt status    # the drawdown halt and its peak (README §7.3)
    uv run auratrade halt reset     # off, the peak at the broker's equity (after a paper reset)

The kill switch blocks new entries and adds only; it never touches a protective stop, a plan
exit or reconciliation. `kill on` cancels resting entry orders under the order lock; every
order job also checks the flag and cancels any that remain.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, insert, select, update

from auratrade.config import RiskConfig, Settings, TradingMode, load_risk_config
from auratrade.db.flags import HALT_DRAWDOWN, TRADING_ENABLED, read_flags
from auratrade.db.models import Mismatch, SystemFlag
from auratrade.execution import halt as drawdown_halt
from auratrade.execution.broker import Broker
from auratrade.execution.orders import OrderDesk
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
        state = "off (entries allowed)" if flags.trading_enabled else "ON (entries blocked)"
        deps.out(f"kill switch {state}; entries allowed now: {flags.entries_allowed}")
        return 0
    if action == "off":
        _set_trading(deps, True)
        deps.out("kill switch off: entries allowed (halts and checks still apply)")
        return 0
    _set_trading(deps, False)
    deps.out("kill switch ON: no new entries or adds")
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
    deps.out(f"cancelled {count} resting entry orders")
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auratrade", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    k = sub.add_parser("kill", help="the kill switch: entries and adds only")
    k.add_argument("action", choices=("on", "off", "status"))
    sub.add_parser("mismatches", help="list open mismatches")
    r = sub.add_parser("resolve", help="mark a mismatch resolved after fixing it")
    r.add_argument("id", type=int)
    r.add_argument("--note", required=True)
    h = sub.add_parser("halt", help="the drawdown halt: its state, or reset it")
    h.add_argument("action", choices=("status", "reset"))
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
            case _:
                return resolve(deps, args.id, args.note)
    finally:
        deps.engine.dispose()


if __name__ == "__main__":
    sys.exit(main())

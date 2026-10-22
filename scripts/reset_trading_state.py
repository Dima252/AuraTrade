"""Clear the trading state of a database copied from another environment (task 1.8).

    uv run python scripts/reset_trading_state.py --env-file .env.paper            # plan
    uv run python scripts/reset_trading_state.py --env-file .env.paper --apply    # clear

A Neon branch made from `dev` carries `dev`'s positions, orders, signals, mismatches and job
runs. Against a different Alpaca account (README §12) they would read as mismatches and
unprotected positions, and the chain would skip sessions `dev` already ran. This deletes them in
one transaction and puts the flags back to their seed: **the kill switch on** (entries off), no
halts, so the new environment trades only after `auratrade kill off`.

Market data stays: bars, tickers, sessions, corporate actions, macro, earnings, fundamentals,
bases and the regime history are the same for every environment.

Refused when the target is the `dev` database (`--dev-env-file`, default `.env`), and when it is
on localhost unless `--allow-local` says that is intended (a cloud branch is the usual target).
Prints table names and row counts, never the URL.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import Connection, Table, delete, func, insert, select
from sqlalchemy.engine import make_url

from auratrade.db.engine import make_engine
from auratrade.db.flags import SEED
from auratrade.db.models import Base, SystemFlag

TRADING_TABLES = frozenset(
    {
        "job_runs",
        "mismatches",
        "signals",
        "orders",
        "positions",
        "position_plans",
        "position_events",
        "trade_journal",
        "account_snapshots",
        "system_flags",
    }
)
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class RefusedError(Exception):
    pass


def trading_tables() -> list[Table]:
    """The tables to clear, children before parents."""
    return [t for t in reversed(Base.metadata.sorted_tables) if t.name in TRADING_TABLES]


def check_target(url: str, dev_url: str | None, *, allow_local: bool) -> None:
    target = make_url(url)
    if dev_url:
        dev = make_url(dev_url)
        if (target.host, target.port, target.database) == (dev.host, dev.port, dev.database):
            raise RefusedError("the target is the dev database")
    if target.host in LOCAL_HOSTS and not allow_local:
        raise RefusedError("the target is on localhost; pass --allow-local if that is intended")


def counts(conn: Connection) -> dict[str, int]:
    return {
        t.name: int(conn.execute(select(func.count()).select_from(t)).scalar_one())
        for t in trading_tables()
    }


def reset(conn: Connection) -> None:
    for table in trading_tables():
        conn.execute(delete(table))
    conn.execute(insert(SystemFlag), [{"name": n, "value": v} for n, v in SEED.items()])


def _url(path: Path) -> str | None:
    if not path.is_file():
        return None
    value = dotenv_values(path).get("DATABASE_URL")
    return value.strip() if value else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--dev-env-file", type=Path, default=Path(".env"))
    parser.add_argument("--apply", action="store_true", help="delete (default: plan only)")
    parser.add_argument("--allow-local", action="store_true")
    args = parser.parse_args(argv)

    url = _url(args.env_file)
    if url is None:
        print(f"no DATABASE_URL in {args.env_file}", file=sys.stderr)
        return 2
    try:
        check_target(url, _url(args.dev_env_file), allow_local=args.allow_local)
    except RefusedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    engine = make_engine(url)
    try:
        with engine.begin() as conn:
            before = counts(conn)
            for name, n in sorted(before.items()):
                print(f"{'clear' if args.apply else 'would clear'} {name}: {n} rows")
            if not args.apply:
                print("plan only: run again with --apply to clear (kill switch goes on)")
                return 0
            reset(conn)
        print("done: trading state cleared, flags reset (kill switch on)")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())

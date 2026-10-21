"""`client_order_id`s (README §5.7.6): how the order layer finds its own orders again.

Alpaca keeps ids unique for the account's lifetime and rejects a duplicate even after the first
order was cancelled (spike 0.3b), so an id is the idempotency key:

- **Entries** are deterministic: `at-20261022-NVDA-e1-1` is signal NVDA's tranche 1 for that
  session, attempt 1. A retried job resubmits the same id, gets `client_order_id must be
  unique`, and looks the order up instead of buying twice.
- **Stops and sells** carry a random suffix. A job decides them from the broker state it just
  reconciled, so a re-run decides afresh; within a run, a call retried after a dropped
  connection reuses its id (`orders.submit_once`).

Leg ids are set by Alpaca (UUIDs), so legs are found through their parent, never by prefix.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

PREFIX = "at"


class Purpose(StrEnum):
    ENTRY = "e"
    STOP = "stop"
    EXIT = "x"


@dataclass(frozen=True, slots=True)
class ParsedId:
    session: date
    symbol: str
    purpose: Purpose
    detail: str
    """The tranche for an entry, the exit reason for a sale, empty for a stop."""


def entry_id(session: date, symbol: str, *, tranche: int = 1, attempt: int = 1) -> str:
    return f"{PREFIX}-{session:%Y%m%d}-{symbol}-{Purpose.ENTRY.value}{tranche}-{attempt}"


def stop_id(session: date, symbol: str) -> str:
    return f"{PREFIX}-{session:%Y%m%d}-{symbol}-{Purpose.STOP.value}-{_suffix()}"


def exit_id(session: date, symbol: str, reason: str) -> str:
    return f"{PREFIX}-{session:%Y%m%d}-{symbol}-{Purpose.EXIT.value}{reason}-{_suffix()}"


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def parse(client_order_id: str) -> ParsedId | None:
    """The parts of one of our ids; None for anyone else's (a leg's UUID, a manual order)."""
    parts = client_order_id.split("-")
    if len(parts) != 5 or parts[0] != PREFIX:
        return None
    _, day, symbol, kind, _suffix_or_attempt = parts
    try:
        session = datetime.strptime(day, "%Y%m%d").date()  # noqa: DTZ007 - a date, no time
    except ValueError:
        return None
    if kind == Purpose.STOP.value:
        return ParsedId(session, symbol, Purpose.STOP, "")
    for purpose in (Purpose.ENTRY, Purpose.EXIT):
        if kind.startswith(purpose.value) and len(kind) > len(purpose.value):
            return ParsedId(session, symbol, purpose, kind[len(purpose.value) :])
    return None

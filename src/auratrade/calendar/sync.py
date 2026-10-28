"""Sync `market_sessions` from Alpaca and cross-check it with `pandas_market_calendars`.

- Alpaca is the authority: its sessions are the rows. A row whose times the library
  disagrees with is marked `DISAGREE` (no increase of exposure that day, README §4); a date only the
  library has is reported, since Alpaca's "closed" stands.
- Idempotent: a second run with the same data writes nothing.
- A synced session missing from Alpaca's new answer (an unscheduled closure) is removed,
  but never more than a handful at once: a vendor glitch must not empty the calendar.

    uv run python -m auratrade.calendar.sync [--start 2016-01-01] [--end 2027-12-31]
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

import structlog
from sqlalchemy import Engine, bindparam, delete, insert, select, update

from auratrade.calendar.sources import NY, SessionTimes
from auratrade.db.models import MarketSession

log = structlog.get_logger(__name__)

HISTORY_START = date(2016, 1, 1)
MAX_REMOVALS = 3


class CalendarSyncError(RuntimeError):
    pass


class DisagreementKind(StrEnum):
    MISSING_IN_CHECK = "MISSING_IN_CHECK"  # Alpaca has a session the library doesn't
    ONLY_IN_CHECK = "ONLY_IN_CHECK"  # the library has a session Alpaca doesn't
    TIMES_DIFFER = "TIMES_DIFFER"


@dataclass(frozen=True)
class Disagreement:
    day: date
    kind: DisagreementKind
    detail: str


@dataclass(frozen=True)
class SyncResult:
    start: date
    end: date
    sessions: int
    inserted: int
    updated: int
    removed: tuple[date, ...]
    disagreements: tuple[Disagreement, ...]

    @property
    def changed(self) -> bool:
        return bool(self.inserted or self.updated or self.removed)


def _hhmm(moment: datetime) -> str:
    return moment.astimezone(NY).strftime("%H:%M")


def compare(primary: Sequence[SessionTimes], check: Sequence[SessionTimes]) -> list[Disagreement]:
    """Disagreements over the dates `primary` covers (the library looks further ahead)."""
    if not primary:
        return []
    first, last = min(s.day for s in primary), max(s.day for s in primary)
    ours = {s.day: s for s in primary}
    theirs = {s.day: s for s in check if first <= s.day <= last}
    found: list[Disagreement] = []
    for day in sorted(ours.keys() | theirs.keys()):
        a, b = ours.get(day), theirs.get(day)
        if b is None:
            found.append(Disagreement(day, DisagreementKind.MISSING_IN_CHECK, "library: closed"))
        elif a is None:
            detail = f"Alpaca: closed; library: {_hhmm(b.open_at)}-{_hhmm(b.close_at)}"
            found.append(Disagreement(day, DisagreementKind.ONLY_IN_CHECK, detail))
        elif (a.open_at, a.close_at) != (b.open_at, b.close_at):
            detail = (
                f"Alpaca {_hhmm(a.open_at)}-{_hhmm(a.close_at)}; "
                f"library {_hhmm(b.open_at)}-{_hhmm(b.close_at)} ET"
            )
            found.append(Disagreement(day, DisagreementKind.TIMES_DIFFER, detail))
    return found


def sync_market_sessions(
    engine: Engine,
    primary: Sequence[SessionTimes],
    check: Sequence[SessionTimes],
    *,
    start: date,
    end: date,
    now: datetime,
    max_removals: int = MAX_REMOVALS,
) -> SyncResult:
    """Write Alpaca's sessions in [start, end] with the cross-check result."""
    primary = [s for s in primary if start <= s.day <= end]
    if not primary:
        raise CalendarSyncError(f"Alpaca returned no sessions for {start} to {end}")
    horizon = max(s.day for s in primary)
    disagreements = compare(primary, check)
    disagree_on = {
        d.day: d.detail for d in disagreements if d.kind is not DisagreementKind.ONLY_IN_CHECK
    }

    wanted = {
        s.day: {
            "open_at": s.open_at,
            "close_at": s.close_at,
            "is_early_close": s.is_early_close,
            "minutes": s.minutes,
            "calendar_check": "DISAGREE" if s.day in disagree_on else "AGREE",
            "calendar_check_detail": disagree_on.get(s.day),
        }
        for s in primary
    }
    t = MarketSession
    columns = [t.session_date, t.open_at, t.close_at, t.is_early_close, t.minutes]
    columns += [t.calendar_check, t.calendar_check_detail]
    inserted = updated = 0
    with engine.begin() as conn:
        existing = {
            row.session_date: dict(row._mapping)
            for row in conn.execute(select(*columns).where(t.session_date.between(start, horizon)))
        }
        removed = tuple(sorted(existing.keys() - wanted.keys()))
        if len(removed) > max_removals:
            raise CalendarSyncError(
                f"Alpaca dropped {len(removed)} synced sessions (limit {max_removals}); "
                "nothing written, check the calendar by hand"
            )
        # Batched: one statement per kind of change, since row by row a full sync took about
        # 8 minutes from a PC to Neon.
        new_rows = []
        changed_rows = []
        for day, values in wanted.items():
            current = existing.get(day)
            if current is None:
                new_rows.append({"session_date": day, "synced_at": now, **values})
            elif any(current[k] != v for k, v in values.items()):
                changed_rows.append({"session_date": day, "synced_at": now, **values})
        if new_rows:
            conn.execute(insert(t), new_rows)
            inserted = len(new_rows)
        if changed_rows:
            # executemany UPDATE: bound names must differ from the column names.
            conn.execute(
                update(t)
                .where(t.session_date == bindparam("p_session_date"))
                .values({c: bindparam(f"p_{c}") for c in changed_rows[0] if c != "session_date"}),
                [{f"p_{k}": v for k, v in row.items()} for row in changed_rows],
            )
            updated = len(changed_rows)
        if removed:
            conn.execute(delete(t).where(t.session_date.in_(removed)))
    result = SyncResult(
        start=start,
        end=horizon,
        sessions=len(primary),
        inserted=inserted,
        updated=updated,
        removed=removed,
        disagreements=tuple(disagreements),
    )
    log.info(
        "calendar_synced",
        start=start.isoformat(),
        end=horizon.isoformat(),
        sessions=result.sessions,
        inserted=inserted,
        updated=updated,
        removed=[d.isoformat() for d in removed],
        disagreements=len(disagreements),
    )
    return result


def alert_text(result: SyncResult) -> str | None:
    """The Telegram alert for a sync, or None when there is nothing to report."""
    lines = [f"{d.day} {d.kind.value}: {d.detail}" for d in result.disagreements]
    lines += [f"{d} REMOVED: no longer a session (closure)" for d in result.removed]
    if not lines:
        return None
    return "Calendar check: no increase of exposure on these sessions\n" + "\n".join(lines)


# --- CLI ---------------------------------------------------------------------------------

Fetch = Callable[[date, date], list[SessionTimes]]


def run_sync(
    engine: Engine, fetch_primary: Fetch, fetch_check: Fetch, start: date, end: date
) -> SyncResult:
    return sync_market_sessions(
        engine,
        fetch_primary(start, end),
        fetch_check(start, end),
        start=start,
        end=end,
        now=datetime.now(UTC),
    )


def main(argv: Sequence[str] | None = None) -> int:
    from auratrade.calendar.sources import AlpacaCalendarClient, exchange_calendar_sessions
    from auratrade.config import load_settings
    from auratrade.db.engine import engine_from_settings
    from auratrade.log import configure_logging

    today = datetime.now(NY).date()
    parser = argparse.ArgumentParser(prog="python -m auratrade.calendar.sync")
    parser.add_argument("--start", type=date.fromisoformat, default=HISTORY_START)
    parser.add_argument("--end", type=date.fromisoformat, default=date(today.year + 1, 12, 31))
    args = parser.parse_args(argv)

    settings = load_settings()
    configure_logging(settings.secret_values())
    if settings.alpaca_api_key is None or settings.alpaca_secret_key is None:
        log.error("calendar_sync_failed", error="ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
        return 1
    alpaca = AlpacaCalendarClient(
        settings.alpaca_api_key.get_secret_value(), settings.alpaca_secret_key.get_secret_value()
    )
    engine = engine_from_settings(settings)
    try:
        result = run_sync(engine, alpaca.fetch, exchange_calendar_sessions, args.start, args.end)
    except Exception as exc:
        log.error("calendar_sync_failed", error=f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        engine.dispose()
    if (text := alert_text(result)) is not None:
        log.warning("calendar_disagreement", alert=text)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

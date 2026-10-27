"""Classify sessions from the database: the evening step and the replay that seeds it."""

from collections.abc import Sequence
from datetime import date

from sqlalchemy import Connection

from auratrade.calendar.sessions import load_calendar
from auratrade.config import RiskConfig
from auratrade.regime.classifier import RegimeResult, classify
from auratrade.regime.inputs import MissingDataError, build_inputs
from auratrade.regime.state import RegimeState
from auratrade.regime.store import load_data, read_state, write_results


def compute(
    conn: Connection,
    risk: RiskConfig,
    targets: Sequence[date],
    state: RegimeState | None,
) -> list[RegimeResult]:
    """Classify consecutive ``targets``, starting from ``state`` (the state after the session
    before ``targets[0]``; ``None`` starts the machine)."""
    if not targets:
        return []
    calendar = load_calendar(conn)
    data = load_data(conn, calendar, targets[0], targets[-1], risk.regime)
    results = []
    for inputs in build_inputs(data, targets, risk.regime):
        result = classify(
            state, inputs, risk.regime, macro_stale_limit=risk.data.macro_stale_sessions
        )
        results.append(result)
        state = result.state
    return results


def run_session(conn: Connection, risk: RiskConfig, session: date) -> RegimeResult:
    """The evening step: continue from the previous session's stored state."""
    previous = load_calendar(conn).previous_session(session).day
    state = read_state(conn, previous)
    if state is None:
        raise MissingDataError(
            f"no market_regime row for {previous}: seed it with "
            f"`python -m auratrade.jobs.regime --from <first session> --session {previous}`"
        )
    [result] = compute(conn, risk, [session], state)
    write_results(conn, [result], risk.config_hash)
    return result


def replay(
    conn: Connection,
    risk: RiskConfig,
    first_label: date,
    last: date,
) -> list[RegimeResult]:
    """Run the machine from ``replay_warmup_sessions`` before ``first_label`` (starting in
    ``CORRECTION``) through ``last``; write the rows from ``first_label`` on."""
    calendar = load_calendar(conn)
    if calendar.first is None:
        raise MissingDataError("market_sessions is empty: sync the calendar")
    days = [s.day for s in calendar.sessions_between(calendar.first, last)]
    if first_label not in days:
        raise ValueError(f"{first_label} is not a session")
    start = days.index(first_label) - risk.regime.replay_warmup_sessions
    if start < 1:
        raise MissingDataError(
            f"a replay labelling {first_label} needs {risk.regime.replay_warmup_sessions} "
            "warm-up sessions (and one before them) in the calendar"
        )
    results = compute(conn, risk, days[start:], None)
    labelled = [r for r in results if r.session >= first_label]
    write_results(conn, labelled, risk.config_hash)
    return labelled

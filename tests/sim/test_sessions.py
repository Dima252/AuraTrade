"""The simulator's sessions for one symbol (sim.sessions)."""

from datetime import UTC, date, datetime, timedelta

from auratrade.config import Regime, Strategy, load_risk_config
from auratrade.indicators.bars import Bar
from auratrade.plans.cushion import EarningsTiming
from auratrade.sim.sessions import (
    CalendarDay,
    Market,
    Minutes,
    SymbolDays,
    cushion_due,
    rs_states,
    sim_sessions,
)
from tests.sim.helpers import M

RISK = load_risk_config()
VCP = Strategy.VCP_BREAKOUT
DAYS = [date(2026, 1, 1) + timedelta(days=i) for i in range(300)]
CU, UP, CORR = Regime.CONFIRMED_UPTREND, Regime.UNDER_PRESSURE, Regime.CORRECTION


def calendar(early: frozenset[date] = frozenset()) -> list[CalendarDay]:
    out = []
    for d in DAYS:
        open_at = datetime(d.year, d.month, d.day, 14, 30, tzinfo=UTC)
        close = open_at + timedelta(hours=3 if d in early else 6, minutes=30)
        out.append(CalendarDay(d, open_at, close, d in early))
    return out


def market(regimes: dict[date, Regime] | None = None, **kw: object) -> Market:
    return Market(
        calendar=kw.pop("calendar", calendar()),  # type: ignore[arg-type]
        regimes=regimes if regimes is not None else dict.fromkeys(DAYS, CU),
        crises=kw.pop("crises", {}),  # type: ignore[arg-type]
        benchmark=dict.fromkeys(DAYS, 100.0),
    )


SYMBOL = SymbolDays(DAYS, [Bar(50, 51, 49, 50, 1e6)] * len(DAYS))


def sessions(days: list[date], m: Market | None = None, **kw: object) -> list:
    return sim_sessions(
        VCP,
        days,
        symbol=kw.pop("symbol", SYMBOL),  # type: ignore[arg-type]
        minutes=kw.pop("minutes", {}),  # type: ignore[arg-type]
        market=m or market(),
        risk=RISK,
    )


def test_daily_sessions_without_minutes() -> None:
    [s] = sessions([DAYS[100]])
    assert (s.has_minutes, s.open_at, s.decision_at, s.tranche2_allowed) == (
        False,
        None,
        None,
        True,
    )


def test_minutes_set_the_open_and_the_1540_decision_time() -> None:
    day = DAYS[100]
    bar = M(datetime(2026, 4, 11, 14, 30, tzinfo=UTC), 50, 51, 49, 50)
    [s] = sessions([day], minutes={day: Minutes([bar], None)})
    assert s.open_at == calendar()[100].open_at
    assert s.decision_at == calendar()[100].close_at - timedelta(minutes=20)
    assert s.sip == [bar]


def test_a_downgrade_reaches_the_next_review_but_not_the_entry_sessions() -> None:
    regimes = dict.fromkeys(DAYS, CU) | {DAYS[100]: UP, DAYS[101]: UP}
    out = sessions(DAYS[101:104], market(regimes))
    assert [s.regime_downgrade for s in out] == [None, None, None]
    out = sessions(DAYS[100:103], market(regimes))
    assert [s.regime_downgrade for s in out] == [None, UP, None]


def test_tranche_2_needs_a_funding_regime_no_crisis_and_a_full_day() -> None:
    regimes = dict.fromkeys(DAYS, CU) | {DAYS[100]: CORR}
    crises = {DAYS[102]: True}
    m = market(regimes, crises=crises, calendar=calendar(frozenset({DAYS[105]})))
    allowed = [s.tranche2_allowed for s in sessions(DAYS[101:106], m)]
    assert allowed == [False, True, False, True, False]


def test_a_day_without_a_bar_ends_the_sessions() -> None:
    gappy = SymbolDays(DAYS[:150], SYMBOL.bars[:150])
    assert len(sessions(DAYS[148:153], symbol=gappy)) == 2


def test_rs_states_need_a_years_line() -> None:
    states = rs_states(SYMBOL, market(), RISK)
    assert DAYS[250] not in states
    assert states[DAYS[251]].near_high


def test_the_cushion_before_a_known_report() -> None:
    reports = [(DAYS[50], EarningsTiming.AFTER_CLOSE), (DAYS[120], EarningsTiming.BEFORE_OPEN)]
    assert cushion_due(DAYS[119], reports, DAYS, RISK)
    assert not cushion_due(DAYS[118], reports, DAYS, RISK)
    after_close = [(DAYS[120], EarningsTiming.AFTER_CLOSE)]
    assert cushion_due(DAYS[120], after_close, DAYS, RISK)


def test_the_cushion_before_an_estimated_report() -> None:
    g = RISK.guardrails
    last = [(DAYS[50], EarningsTiming.BEFORE_OPEN)]
    due = DAYS[50 + g.earnings_estimate_sessions - g.earnings_estimate_window_sessions - 1]
    assert cushion_due(due, last, DAYS, RISK)
    assert not cushion_due(DAYS[60], last, DAYS, RISK)
    assert not cushion_due(DAYS[60], [], DAYS, RISK)

"""The simulator's sessions for one symbol (``sim.trade.SimSession``), pure: what the evening
`simulate` job knows about each session of a simulated trade.

- **Times:** the open, and the decision time ``DECISION_BEFORE_CLOSE`` before the close (15:40,
  12:40 on a half day: the `eod_review` slots).
- **Minute bars** where the caller has them (SIP for fills, IEX for levels); without them the
  session runs on its daily bar, the close standing in for the decision price.
- **Regime:** the computed regime written at the evening before each session governs it. A
  downgrade between the two evenings before a session reaches its review (``regime_downgrade``;
  the entry session's own review has none: its entry was decided on that regime). Tranche 2 is
  allowed while the strategy is fundable in that regime, with no crisis override, and not on a
  half day (README §4).
- **Earnings cushion:** due at the review before a known report (``plans.cushion``), or, with
  no known next report, before the window estimated from the last one.
- **RS line:** close / SPY, at a 50-session low (faltering RS) or near its 52-week high (the
  tranche-2 gate), from the symbol's own history before the trade.
- **Not modelled:** the 15:40 volume projection (the day's full volume stands in) and the
  base count of a new breakout (no base table yet).
"""

from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from auratrade.config import Regime, RiskConfig, Strategy
from auratrade.indicators.bars import OHLCV
from auratrade.indicators.relative_strength import RsLineState, rs_line_states
from auratrade.plans.cushion import EarningsTiming, cushion_session, estimated_cushion_session
from auratrade.regime.ladder import rank
from auratrade.sim.fills import MinuteBar
from auratrade.sim.trade import SimSession
from auratrade.strategies.registry import fundable

DECISION_BEFORE_CLOSE = timedelta(minutes=20)


@dataclass(frozen=True, slots=True)
class CalendarDay:
    day: date
    open_at: datetime
    close_at: datetime
    early_close: bool = False

    @property
    def decision_at(self) -> datetime:
        return self.close_at - DECISION_BEFORE_CLOSE


@dataclass(frozen=True, slots=True)
class Minutes:
    sip: Sequence[MinuteBar]
    iex: Sequence[MinuteBar] | None
    """None: no IEX history (before 2020-07-27), so SIP sets the levels."""


@dataclass(frozen=True, slots=True)
class Market:
    """The calendar and the market-wide facts, the same for every symbol."""

    calendar: Sequence[CalendarDay]
    """Every session the trades span, plus the evenings before them, oldest first."""
    regimes: Mapping[date, Regime]
    """The computed regime written at each session's close."""
    crises: Mapping[date, bool]
    benchmark: Mapping[date, float]
    """SPY's closes."""


@dataclass(frozen=True, slots=True)
class SymbolDays:
    """One symbol's daily bars (history and trade sessions), oldest first, and its reports."""

    dates: Sequence[date]
    bars: Sequence[OHLCV]
    reports: Sequence[tuple[date, EarningsTiming]] = ()


def rs_states(symbol: SymbolDays, market: Market, risk: RiskConfig) -> dict[date, RsLineState]:
    """The RS-line state on each of the symbol's sessions that has a benchmark close."""
    pairs = [
        (d, bar.close / market.benchmark[d])
        for d, bar in zip(symbol.dates, symbol.bars, strict=True)
        if d in market.benchmark
    ]
    cfg = risk.ranking.rs_line
    states = rs_line_states(
        [v for _, v in pairs],
        high_window=cfg.high_window,
        near_tolerance=cfg.near_tolerance,
        low_window=cfg.low_window,
    )
    return {d: s for (d, _), s in zip(pairs, states, strict=True) if s is not None}


def cushion_due(
    day: date,
    reports: Sequence[tuple[date, EarningsTiming]],
    sessions: Sequence[date],
    risk: RiskConfig,
) -> bool:
    """``day``'s review runs the earnings cushion: before the next known report, or before the
    window estimated from the last one when no later report is known."""
    upcoming = [r for r in reports if r[0] >= day]
    if upcoming:
        report, timing = min(upcoming)
        return cushion_session(report, timing, sessions) == day
    past = [r[0] for r in reports if r[0] < day]
    if not past:
        return False
    g = risk.guardrails
    estimated = estimated_cushion_session(
        max(past),
        sessions,
        estimate_sessions=g.earnings_estimate_sessions,
        window_sessions=g.earnings_estimate_window_sessions,
    )
    return estimated == day


def sim_sessions(
    strategy: Strategy,
    days: Sequence[date],
    *,
    symbol: SymbolDays,
    minutes: Mapping[date, Minutes],
    market: Market,
    risk: RiskConfig,
    tested_ma: float | None = None,
) -> list[SimSession]:
    """The trade's sessions ``days`` (from its entry session), each with its daily bar; days
    the symbol has no bar on end the list (halted or delisted: the trade is marked there)."""
    calendar = [c.day for c in market.calendar]
    times = {c.day: c for c in market.calendar}
    bars = dict(zip(symbol.dates, symbol.bars, strict=True))
    rs = rs_states(symbol, market, risk)
    out: list[SimSession] = []
    for n, day in enumerate(days, start=1):
        bar = bars.get(day)
        if bar is None:
            break
        i = bisect_left(calendar, day)
        evening = market.regimes.get(calendar[i - 1]) if i >= 1 else None
        before = market.regimes.get(calendar[i - 2]) if i >= 2 else None
        downgraded = (
            evening
            if n > 1 and evening is not None and before is not None and rank(evening) < rank(before)
            else None
        )
        allowed = (
            evening is not None
            and any(f.strategy is strategy for f in fundable(risk, evening))
            and not market.crises.get(calendar[i - 1], False)
            and not times[day].early_close
        )
        state = rs.get(day)
        found = minutes.get(day)
        t = times[day]
        out.append(
            SimSession(
                bar=bar,
                open_at=t.open_at if found else None,
                decision_at=t.decision_at if found else None,
                sip=found.sip if found else (),
                iex=found.iex if found else None,
                rs_line_at_low=state.at_low if state else False,
                rs_line_near_high=state.near_high if state else False,
                tested_ma=tested_ma,
                regime_downgrade=downgraded,
                earnings_cushion_due=cushion_due(day, symbol.reports, calendar, risk),
                tranche2_allowed=allowed,
            )
        )
    return out

"""The progressive-exposure multiplier (archived README §8), pure.

Evaluated once a day in ``post_market``: ``step`` takes yesterday's state and today's closed
trades and equity, and returns today's state and reading. **The first matching row wins**:

1. Drawdown >= 10% from the equity peak, until it recovers below 10% or 3 wins in the tier: 0.25
2. Drawdown >= 5%, until it recovers below 5% or 3 wins in the tier: 0.5
3. Losing streak: 3 losses in a row; ends after 2 wins in a row or a new equity high: 0.5
4. Prove it first: at the start or after a manual drawdown-halt reset; ends after 2 wins: 0.5
5. Unproven regime (live only): < 5 closed trades in the effective regime: 0.5
6. Trading well: a new equity high in the last 5 sessions, and positive expectancy over the
   last 10 closed trades (needs >= 10): 1.25
7. Normal: 1.0

- A trade's result is its realized R: a **win** at >= +0.5R, a **loss** at <= -0.5R, otherwise
  a **scratch**, which neither counts nor breaks a streak.
- **Leaving a drawdown tier.** A tier's wins count only while it is the row in force. A tier
  ended by wins applies again only if equity makes a new low (below its low when it ended);
  until then the next matching row applies, so size steps back up one tier at a time. A
  recovery past the threshold resets the tier.
- Only a manual drawdown-halt reset restarts "prove it first"; the automatic daily and weekly
  halt resets don't.
- Within a day, the closed trades count first (against the row in force since yesterday), then
  the equity moves the peak, the drawdown and the tiers.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from auratrade.config import MultiplierConfig
from auratrade.plans.model import reached


class TradeResult(StrEnum):
    WIN = "WIN"
    LOSS = "LOSS"
    SCRATCH = "SCRATCH"


def trade_result(r: float, config: MultiplierConfig) -> TradeResult:
    if r >= config.win_r:
        return TradeResult.WIN
    if r <= -config.loss_r:
        return TradeResult.LOSS
    return TradeResult.SCRATCH


class Row(StrEnum):
    """Archived README §8, most conservative first."""

    DEEP_DRAWDOWN = "DEEP_DRAWDOWN"
    DRAWDOWN = "DRAWDOWN"
    LOSING_STREAK = "LOSING_STREAK"
    PROVE_IT = "PROVE_IT"
    UNPROVEN_REGIME = "UNPROVEN_REGIME"
    TRADING_WELL = "TRADING_WELL"
    NORMAL = "NORMAL"


def row_multiplier(row: Row, config: MultiplierConfig) -> float:
    return {
        Row.DEEP_DRAWDOWN: config.deep_drawdown_multiplier,
        Row.DRAWDOWN: config.drawdown_multiplier,
        Row.LOSING_STREAK: config.losing_streak_multiplier,
        Row.PROVE_IT: config.prove_it_multiplier,
        Row.UNPROVEN_REGIME: config.unproven_regime_multiplier,
        Row.TRADING_WELL: config.trading_well_multiplier,
        Row.NORMAL: 1.0,
    }[row]


@dataclass(frozen=True, slots=True)
class Tier:
    """One drawdown tier."""

    active: bool = False
    wins: int = 0
    """Wins while this tier was the row in force."""
    ended_low: float | None = None
    """Ended by wins at this equity low; applies again only below it."""


@dataclass(frozen=True, slots=True)
class MultiplierState:
    session: int
    """Sessions evaluated so far."""
    peak: float
    trough: float
    """The lowest equity since the peak."""
    row: Row
    """The row in force after the last evaluation."""
    deep: Tier = Tier()
    shallow: Tier = Tier()
    losses_in_row: int = 0
    streak: bool = False
    streak_wins: int = 0
    """Wins in a row while the losing streak is in force."""
    prove_it: bool = True
    prove_it_wins: int = 0
    last_high_session: int | None = None
    recent_r: tuple[float, ...] = ()
    """The last ``trading_well_trades`` closed trades' R."""
    closed_trades: int = 0

    @classmethod
    def start(cls, equity: float) -> "MultiplierState":
        """A new account: "prove it first"."""
        if not equity > 0:
            raise ValueError(f"equity must be positive, got {equity}")
        return cls(session=0, peak=equity, trough=equity, row=Row.PROVE_IT)


@dataclass(frozen=True, slots=True)
class Day:
    equity: float
    """The account equity after the session."""
    trade_rs: tuple[float, ...] = ()
    """Realized R of the trades closed this session, in closing order."""
    drawdown_halt_reset: bool = False
    """The drawdown halt was re-enabled by hand this session."""
    regime_closed_trades: int | None = None
    """Live only: closed trades (paper trial and live) in today's effective regime."""


@dataclass(frozen=True, slots=True)
class Reading:
    row: Row
    multiplier: float


def _count_trade(state: MultiplierState, r: float, config: MultiplierConfig) -> MultiplierState:
    result = trade_result(r, config)
    recent = (*state.recent_r, r)[-config.trading_well_trades :]
    state = replace(state, recent_r=recent, closed_trades=state.closed_trades + 1)
    if result is TradeResult.SCRATCH:
        return state
    win = result is TradeResult.WIN
    if win and state.row is Row.DEEP_DRAWDOWN:
        state = replace(state, deep=replace(state.deep, wins=state.deep.wins + 1))
    elif win and state.row is Row.DRAWDOWN:
        state = replace(state, shallow=replace(state.shallow, wins=state.shallow.wins + 1))

    losses = 0 if win else state.losses_in_row + 1
    streak, streak_wins = state.streak, state.streak_wins + 1 if win else 0
    if losses >= config.losing_streak:
        streak, streak_wins = True, 0
    elif streak and streak_wins >= config.losing_streak_exit_wins:
        streak, streak_wins = False, 0
    prove_wins = state.prove_it_wins + (1 if win and state.prove_it else 0)
    return replace(
        state,
        losses_in_row=losses,
        streak=streak,
        streak_wins=streak_wins if streak else 0,
        prove_it=state.prove_it and prove_wins < config.prove_it_wins,
        prove_it_wins=prove_wins,
    )


def _tier(
    tier: Tier, *, threshold: float, drawdown: float, equity: float, trough: float, wins: int
) -> Tier:
    if not reached(drawdown, threshold):
        return Tier()  # recovered past the threshold
    if tier.ended_low is not None:
        return Tier(active=True) if equity < tier.ended_low else tier
    if tier.wins >= wins:
        return Tier(wins=tier.wins, ended_low=trough)
    return Tier(active=True, wins=tier.wins)


def _row(state: MultiplierState, day: Day, config: MultiplierConfig) -> Row:
    if state.deep.active:
        return Row.DEEP_DRAWDOWN
    if state.shallow.active:
        return Row.DRAWDOWN
    if state.streak:
        return Row.LOSING_STREAK
    if state.prove_it:
        return Row.PROVE_IT
    if day.regime_closed_trades is not None and (
        day.regime_closed_trades < config.unproven_regime_trades
    ):
        return Row.UNPROVEN_REGIME
    recent_high = (
        state.last_high_session is not None
        and state.session - state.last_high_session < config.trading_well_high_sessions
    )
    if (
        recent_high
        and len(state.recent_r) >= config.trading_well_trades
        and sum(state.recent_r) > 0
    ):
        return Row.TRADING_WELL
    return Row.NORMAL


def step(
    state: MultiplierState, day: Day, config: MultiplierConfig
) -> tuple[MultiplierState, Reading]:
    """Today's state and multiplier."""
    if not day.equity > 0:
        raise ValueError(f"equity must be positive, got {day.equity}")
    for r in day.trade_rs:
        state = _count_trade(state, r, config)
    if day.drawdown_halt_reset:
        state = replace(state, prove_it=True, prove_it_wins=0)

    session = state.session + 1
    if day.equity > state.peak:
        state = replace(
            state,
            peak=day.equity,
            trough=day.equity,
            last_high_session=session,
            streak=False,
            streak_wins=0,
            losses_in_row=0,
        )
    state = replace(state, session=session, trough=min(state.trough, day.equity))
    drawdown = 1 - day.equity / state.peak

    def tier(tier: Tier, threshold: float) -> Tier:
        return _tier(
            tier,
            threshold=threshold,
            drawdown=drawdown,
            equity=day.equity,
            trough=state.trough,
            wins=config.tier_exit_wins,
        )

    state = replace(
        state,
        deep=tier(state.deep, config.deep_drawdown),
        shallow=tier(state.shallow, config.drawdown),
    )
    row = _row(state, day, config)
    return replace(state, row=row), Reading(row, row_multiplier(row, config))


def replay(
    equity: float, days: Sequence[Day], config: MultiplierConfig
) -> tuple[MultiplierState, list[Reading]]:
    """Every day's reading from a new account's starting equity (backtests, tests)."""
    state = MultiplierState.start(equity)
    readings = []
    for day in days:
        state, reading = step(state, day, config)
        readings.append(reading)
    return state, readings

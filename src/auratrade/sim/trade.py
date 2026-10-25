"""One approved entry played forward through the fill model (README §5.12), pure.

The evening baseline, the verdict scorecard, the 3.8 backtest and the plan counterfactual all
call ``simulate_trade``. It calls the live pure functions, never copies of them: the 09:36 order
check (``guardrails.orders``: trigger, limit, stop, stop width, whole-share sizing, tranches),
the plan written at entry, ``plans.resolve`` at each session's decision price, the tranche-2
gate and guardrail, and ``execution.sizing.tranche2_shares``.

**The entry session** needs minute bars. Levels (the opening range, the 09:36 last price, the
low of the day, the decision price) come from IEX bars, fills from SIP bars. Without IEX history
(before 2020-07-27) the SIP bars set the levels too. When IEX printed nothing in the opening
range of an opening-range entry, the consolidated range is used and the order goes in at 09:51
(#109). The entry works until the decision time, when the EOD review cancels it (§5.7).

**A close entry** (the #144 candidate, ``TriggerKind.CLOSE``) needs no minute bars: on its entry
session the strategy's gate is read at the decision time (``plans.entry.entry_gate``), then
``guardrails.orders.check_close`` sets the limit, the stop and the size, and the buy fills at
the decision price plus slippage if that is within the limit: the tranche-2 fill. The session
then runs only the hard stop from the decision time to the close.

**Later sessions** may carry minute bars (the evening simulation) or only a daily bar
(backtests, where the close stands in for the decision price, §7.3). Each session runs:
1. the hard stop, bar by bar, up to the decision time (on a daily bar: the whole bar)
2. at the decision time, the plan's rules on the decision price, then tranche 2
3. after it, to the close, the hard stop again at its new level

Kept simple: stop-raising milestones apply at the decision time (live, the intraday monitor
raises them when reached); orders fill whole; a session without a decision price runs only the
hard stop, as live (a soft exit waits, §7.3).

R is the trade's P&L over the dollars it put at risk: every buy's shares x (fill - initial stop).
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum

from auratrade.config import Regime, RiskConfig, TriggerKind
from auratrade.execution.prices import round_up
from auratrade.execution.sizing import tranche2_shares
from auratrade.execution.triggers import OrderKind, opening_range, session_low
from auratrade.guardrails.evaluate import ApprovedEntry
from auratrade.guardrails.levels import StrategyRecord
from auratrade.guardrails.model import Finding, GuardrailRule, reject
from auratrade.guardrails.orders import (
    MarketAtDecision,
    MarketAtOpen,
    OrderPlan,
    check_close,
    check_order,
)
from auratrade.guardrails.positions import tranche2_check
from auratrade.indicators.bars import OHLCV, Bar
from auratrade.indicators.climax import climax_signals
from auratrade.indicators.moving_averages import sma
from auratrade.plans.entry import GateInput, GateResult, entry_gate, new_plan, tranche2_gate
from auratrade.plans.levels import trail_levels
from auratrade.plans.model import Day, Event, PositionPlan, Rule
from auratrade.plans.resolve import resolve
from auratrade.plans.versioning import with_tranche2
from auratrade.sim.fills import (
    MinuteBar,
    PriceBar,
    between,
    buy_fill,
    entry_fill,
    last_price,
    sell_fill,
    slippage,
    stop_fill,
)


@dataclass(frozen=True, slots=True)
class SimSession:
    """One session of the trade: its daily bar, minute bars where there are any, and what the
    caller knows about it that the plan rules read."""

    bar: OHLCV
    """The daily bar."""
    open_at: datetime | None = None
    """09:30 ET as an aware datetime; needed with minute bars."""
    decision_at: datetime | None = None
    """15:40 ET (12:40 on a half day); needed with minute bars."""
    sip: Sequence[MinuteBar] = ()
    """Consolidated minute bars: fills. Empty: a daily-bar session."""
    iex: Sequence[MinuteBar] | None = None
    """IEX minute bars: levels. None: no IEX history, so SIP sets the levels."""
    decision_price: float | None = None
    """Overrides the decision price taken from the minute bars (or the close)."""
    projected_volume: float | None = None
    """The day's volume projected at the decision time; the daily volume if not given."""
    rs_line_at_low: bool = False
    rs_line_near_high: bool = False
    tested_ma: float | None = None
    """The 10/21 EMA a pullback turned off (the pullback gate)."""
    breakout_base_count: int | None = None
    regime_downgrade: Regime | None = None
    earnings_cushion_due: bool = False
    tranche2_allowed: bool = True
    """The strategy is still fundable, with no crisis override, halt or kill switch (§7.1)."""

    @property
    def has_minutes(self) -> bool:
        return bool(self.sip)


@dataclass(frozen=True, slots=True)
class TradeSetup:
    entry: ApprovedEntry
    code_pivot: float
    atr: float | None
    adr: float | None
    """A fraction; an EP's pre-gap ADR."""
    dollar_volume: float | None
    """The 50-session average dollar volume (slippage)."""
    history: Sequence[OHLCV]
    """Daily bars before the entry session, oldest first: enough for the 200-day and the trails
    (about a year)."""


class TradeOutcome(StrEnum):
    SKIPPED = "SKIPPED"
    """The 09:36 check rejected it (``findings`` say why)."""
    NOT_CONFIRMED = "NOT_CONFIRMED"
    """A close entry whose gate failed at the decision time (``gate`` says why)."""
    MISSED = "MISSED"
    """Placed but never filled: not triggered, or it jumped past its limit for good."""
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    """Still held when the sessions ran out; marked at the last close."""


class FillKind(StrEnum):
    ENTRY = "ENTRY"
    TRANCHE_2 = "TRANCHE_2"
    SALE = "SALE"
    EXIT = "EXIT"


@dataclass(frozen=True, slots=True)
class TradeFill:
    session: int
    """1 = the entry session."""
    kind: FillKind
    shares: int
    price: float
    rule: Rule | None = None


@dataclass(frozen=True, slots=True)
class TradeResult:
    outcome: TradeOutcome
    findings: tuple[Finding, ...] = ()
    order: OrderPlan | None = None
    fills: tuple[TradeFill, ...] = ()
    plan: PositionPlan | None = None
    """The last plan version."""
    exit: Rule | None = None
    events: tuple[tuple[int, Event], ...] = ()
    """Each plan event with its session."""
    risk: float = 0.0
    """Dollars put at risk: every buy's shares x (fill - initial stop)."""
    pnl: float = 0.0
    """Realized P&L, plus open shares at the last close."""
    marks: tuple[float, ...] = ()
    """R at each session's close, from the entry session to the exit (or the last session)."""
    gate: GateResult | None = None
    """A close entry's gate at the decision time."""

    @property
    def triggered(self) -> bool:
        return self.outcome in (TradeOutcome.CLOSED, TradeOutcome.OPEN)

    @property
    def r(self) -> float | None:
        return self.pnl / self.risk if self.risk > 0 else None

    def r_after(self, sessions: int) -> float | None:
        """R after ``sessions`` sessions (the scorecard's 5/10/20): the final R once closed,
        None while the data hasn't reached that far."""
        if sessions < 1:
            raise ValueError(f"sessions must be >= 1, got {sessions}")
        if sessions <= len(self.marks):
            return self.marks[sessions - 1]
        return self.r if self.outcome is TradeOutcome.CLOSED else None


@dataclass
class _Trade:
    """The position while it is held."""

    setup: TradeSetup
    order: OrderPlan
    plan: PositionPlan
    risk_config: RiskConfig
    slip: float
    shares: int = 0
    cash: float = 0.0
    risk: float = 0.0
    fills: list[TradeFill] = field(default_factory=list)
    events: list[tuple[int, Event]] = field(default_factory=list)
    marks: list[float] = field(default_factory=list)
    exit: Rule | None = None
    bars: list[OHLCV] = field(default_factory=list)
    """Daily bars: the history, then each finished session's."""
    prior_price: float = 0.0
    """The last session's decision price (its close without one)."""
    high_since_entry: float = 0.0
    heaviest_down_volume: float | None = None

    def buy(self, session: int, kind: FillKind, shares: int, price: float) -> None:
        self.shares += shares
        self.cash -= shares * price
        self.risk += shares * (price - self.plan.initial_stop)
        self.fills.append(TradeFill(session, kind, shares, price))

    def sell(self, session: int, shares: int, price: float, rule: Rule, *, exit: bool) -> None:
        self.shares -= shares
        self.cash += shares * price
        kind = FillKind.EXIT if exit else FillKind.SALE
        self.fills.append(TradeFill(session, kind, shares, price, rule))
        if exit:
            self.exit = rule

    def close_out(self, session: int, price: float, rule: Rule) -> None:
        self.sell(session, self.shares, price, rule, exit=True)

    def mark(self, close: float) -> None:
        self.marks.append((self.cash + self.shares * close) / self.risk)

    def result(self) -> TradeResult:
        last_close = self.bars[-1].close
        return TradeResult(
            outcome=TradeOutcome.CLOSED if self.exit is not None else TradeOutcome.OPEN,
            order=self.order,
            fills=tuple(self.fills),
            plan=self.plan,
            exit=self.exit,
            events=tuple(self.events),
            risk=self.risk,
            pnl=self.cash + self.shares * last_close,
            marks=tuple(self.marks),
        )


def simulate_trade(
    setup: TradeSetup,
    sessions: Sequence[SimSession],
    *,
    equity: float,
    record: StrategyRecord | None,
    risk: RiskConfig,
) -> TradeResult:
    """Play ``setup.entry`` from its entry session (``sessions[0]``) to its exit or the last
    session."""
    if not sessions:
        raise ValueError("no sessions to simulate")
    if not setup.history:
        raise ValueError("the trade needs at least the session before its entry")
    for s in sessions:
        if s.has_minutes and (s.open_at is None or s.decision_at is None):
            raise ValueError("a session with minute bars needs its open and decision times")
    if risk.plans.entry[setup.entry.strategy].trigger is TriggerKind.CLOSE:
        return _close_entry(setup, sessions, equity=equity, record=record, risk=risk)
    first = sessions[0]
    if not first.has_minutes or first.open_at is None or first.decision_at is None:
        raise ValueError("the entry session needs minute bars and its open and decision times")

    slip = slippage(setup.dollar_volume, risk.sim)
    placed = _place(setup, first, first.open_at, equity=equity, record=record, risk=risk)
    if isinstance(placed, TradeResult):
        return placed
    order, order_at = placed

    window = between(first.sip, order_at, first.decision_at)
    trigger = order.trigger if order.kind is OrderKind.STOP_LIMIT else None
    filled = entry_fill(window, trigger=trigger, limit=order.limit, slip=slip)
    if filled is None:
        return TradeResult(TradeOutcome.MISSED, order=order)

    policy = risk.plans.entry[setup.entry.strategy]
    pivot = order.trigger if policy.trigger is TriggerKind.OPENING_RANGE_HIGH else setup.code_pivot
    plan = new_plan(
        setup.entry.strategy,
        pivot=pivot,
        entry_price=filled.price,
        initial_stop=order.stop,
        config=risk.plans,
        exits=setup.entry.exits,
    )
    trade = _Trade(
        setup=setup,
        order=order,
        plan=plan,
        risk_config=risk,
        slip=slip,
        bars=list(setup.history),
        prior_price=setup.history[-1].close,
        high_since_entry=filled.price,
    )
    trade.buy(1, FillKind.ENTRY, order.tranches.first, filled.price)

    for n, s in enumerate(sessions, start=1):
        held = window[filled.index :] if n == 1 else None
        _session(trade, n, s, held)
        if trade.exit is not None:
            break
    return trade.result()


def _close_entry(
    setup: TradeSetup,
    sessions: Sequence[SimSession],
    *,
    equity: float,
    record: StrategyRecord | None,
    risk: RiskConfig,
) -> TradeResult:
    """A close entry from its entry session (``sessions[0]``) to its exit or the last session."""
    first = sessions[0]
    strategy = setup.entry.strategy
    price, bars, after = _decision_view(first, risk)
    if price is None:
        return TradeResult(
            TradeOutcome.SKIPPED,
            findings=(reject(GuardrailRule.INVALID_INPUT, "no decision price"),),
        )
    day_low = min(b.low for b in bars)
    volumes = [b.volume for b in setup.history[-risk.sim.volume_sessions :]]
    volume = first.projected_volume if first.projected_volume is not None else first.bar.volume
    gate = entry_gate(
        strategy,
        GateInput(
            price=price,
            level=setup.code_pivot,
            day_high=max(b.high for b in bars),
            day_low=day_low,
            projected_volume=volume,
            avg_volume_50=(
                statistics.fmean(volumes) if len(volumes) == risk.sim.volume_sessions else None
            ),
            rs_line_near_high=first.rs_line_near_high,
            rs_line_at_low=first.rs_line_at_low,
            tested_ma=first.tested_ma,
        ),
        risk.plans,
    )
    if not gate.passed:
        return TradeResult(TradeOutcome.NOT_CONFIRMED, gate=gate)
    market = MarketAtDecision(price=price, low_of_day=day_low, atr=setup.atr, adr=setup.adr)
    check = check_close(setup.entry, market, equity=equity, record=record, risk=risk)
    if check.plan is None:
        return TradeResult(TradeOutcome.SKIPPED, findings=check.findings, gate=gate)
    order = check.plan
    slip = slippage(setup.dollar_volume, risk.sim)
    fill = buy_fill(price, slip)
    if fill > order.limit:
        return TradeResult(TradeOutcome.MISSED, order=order, gate=gate)

    plan = new_plan(
        strategy,
        pivot=setup.code_pivot,
        entry_price=fill,
        initial_stop=order.stop,
        config=risk.plans,
        exits=setup.entry.exits,
    )
    trade = _Trade(
        setup=setup,
        order=order,
        plan=plan,
        risk_config=risk,
        slip=slip,
        bars=list(setup.history),
        high_since_entry=max([fill, *(b.high for b in after)]),
    )
    trade.buy(1, FillKind.ENTRY, order.tranches.first, fill)
    if (hit := stop_fill(after, stop=order.stop, slip=slip)) is not None:
        trade.close_out(1, hit.price, Rule.HARD_STOP)
    trade.mark(first.bar.close)
    trade.prior_price = price
    trade.bars.append(first.bar)
    for n, s in enumerate(sessions[1:], start=2):
        if trade.exit is not None:
            break
        _session(trade, n, s, None)
    return replace(trade.result(), gate=gate)


def _decision_view(
    s: SimSession, risk: RiskConfig
) -> tuple[float | None, Sequence[PriceBar], Sequence[PriceBar]]:
    """The session's decision price, its bars up to the decision time and its bars after it.
    On a daily bar the close is the decision price and the bar is all before it."""
    if s.has_minutes and s.open_at is not None and s.decision_at is not None:
        levels = s.iex if s.iex is not None else s.sip
        price = (
            s.decision_price
            if s.decision_price is not None
            else _price_at(s, levels, s.decision_at, risk)
        )
        return price, between(s.sip, s.open_at, s.decision_at), between(s.sip, s.decision_at, None)
    price = s.decision_price if s.decision_price is not None else s.bar.close
    return price, [s.bar], []


def _place(
    setup: TradeSetup,
    first: SimSession,
    open_at: datetime,
    *,
    equity: float,
    record: StrategyRecord | None,
    risk: RiskConfig,
) -> tuple[OrderPlan, datetime] | TradeResult:
    """The 09:36 order (09:51 on the delayed-SIP range), or the skip."""
    policy = risk.plans.entry[setup.entry.strategy]
    levels = first.iex if first.iex is not None else first.sip
    order_at = open_at + timedelta(minutes=risk.sim.entry_minutes)
    minutes = risk.entries.opening_range_minutes
    opening = opening_range(levels, open_at=open_at, minutes=minutes)
    if opening is None and policy.trigger is not TriggerKind.PIVOT:
        levels = first.sip
        opening = opening_range(levels, open_at=open_at, minutes=minutes)
        order_at = open_at + timedelta(minutes=risk.sim.delayed_entry_minutes)
    market = MarketAtOpen(
        opening=opening,
        price=_price_at(first, levels, order_at, risk),
        low_of_day=session_low(levels, open_at=open_at, until=order_at),
        code_pivot=setup.code_pivot,
        atr=setup.atr,
        adr=setup.adr,
    )
    check = check_order(setup.entry, market, equity=equity, record=record, risk=risk)
    if check.plan is None:
        return TradeResult(TradeOutcome.SKIPPED, findings=check.findings)
    return check.plan, order_at


def _price_at(
    s: SimSession, levels: Sequence[MinuteBar], at: datetime, risk: RiskConfig
) -> float | None:
    """The last trade on the level feed if fresh; else the last SIP trade, standing in for the
    IEX quote midpoint live trading falls back to (#120: there is no quote history)."""
    max_age = timedelta(seconds=risk.execution.decision_price.trade_max_age_seconds)
    price = last_price(levels, at=at, max_age=max_age)
    return price if price is not None else last_price(s.sip, at=at, max_age=max_age)


def _session(trade: _Trade, n: int, s: SimSession, held: Sequence[PriceBar] | None) -> None:
    """Session ``n`` of a held position; ``held`` is the entry session's bars from the fill."""
    plan_stop = trade.plan.hard_stop
    price, session_bars, after = _decision_view(s, trade.risk_config)
    before = held if held is not None else session_bars

    if (hit := stop_fill(before, stop=plan_stop, slip=trade.slip)) is not None:
        trade.close_out(n, hit.price, Rule.HARD_STOP)
    else:
        trade.high_since_entry = max([trade.high_since_entry, *(b.high for b in before)])
        if price is not None:
            _decide(trade, n, s, price, before, session_bars)
        if trade.exit is None and (
            hit := stop_fill(after, stop=trade.plan.hard_stop, slip=trade.slip)
        ):
            trade.close_out(n, hit.price, Rule.HARD_STOP)
    trade.mark(s.bar.close)
    trade.prior_price = price if price is not None else s.bar.close
    trade.bars.append(s.bar)


def _decide(
    trade: _Trade,
    n: int,
    s: SimSession,
    price: float,
    held: Sequence[PriceBar],
    session_bars: Sequence[PriceBar],
) -> None:
    """The EOD review at the decision time: the plan's rules, then tranche 2."""
    risk = trade.risk_config
    volumes = [b.volume for b in trade.bars[-risk.sim.volume_sessions :]]
    avg_volume = statistics.fmean(volumes) if len(volumes) == risk.sim.volume_sessions else None
    volume = s.projected_volume if s.projected_volume is not None else s.bar.volume
    day_high = max((b.high for b in session_bars), default=s.bar.high)
    day_low = min((b.low for b in session_bars), default=s.bar.low)
    today = Bar(s.bar.open, day_high, day_low, price, volume)
    closes = [b.close for b in trade.bars] + [price]

    day = Day(
        session=n,
        price=price,
        prior_close=trade.prior_price,
        high=max((b.high for b in held), default=price),
        low=min((b.low for b in held), default=price),
        high_since_entry=max(trade.high_since_entry, price),
        volume=volume,
        avg_volume_50=avg_volume,
        heaviest_down_volume=trade.heaviest_down_volume,
        trail_levels=trail_levels(closes, risk.plans),
        climax_signals=_climax(trade, today, closes),
        rs_line_at_low=s.rs_line_at_low,
        breakout_base_count=s.breakout_base_count,
        regime_downgrade=s.regime_downgrade,
        earnings_cushion_due=s.earnings_cushion_due,
    )
    if price < trade.prior_price:
        trade.heaviest_down_volume = max(trade.heaviest_down_volume or 0.0, volume)

    res = resolve(trade.plan, day, shares=trade.shares, config=risk)
    trade.plan = res.plan
    trade.events += [(n, event) for event in res.events]
    exit_price = sell_fill(price, trade.slip)
    if res.exit is not None:
        trade.close_out(n, exit_price, res.exit)
        return
    for sale in res.sales:
        trade.sell(n, sale.shares, exit_price, sale.rule, exit=False)

    gate = GateInput(
        price=price,
        level=trade.plan.pivot,
        day_high=day_high,
        day_low=day_low,
        projected_volume=volume,
        avg_volume_50=avg_volume,
        rs_line_near_high=s.rs_line_near_high,
        rs_line_at_low=s.rs_line_at_low,
        tested_ma=s.tested_ma,
    )
    _tranche2(trade, n, s, price, gate, blocked=res.block_tranche2)


def _climax(trade: _Trade, today: Bar, closes: list[float]) -> int:
    climax = trade.risk_config.climax
    long_average = sma(closes, trade.risk_config.sim.long_average_sessions)
    bars = [*trade.bars, today]
    return climax_signals(
        bars,
        at=len(bars) - 1,
        move_start=len(trade.setup.history),
        long_average=long_average[-1] if long_average else None,
        exhaustion_min_gap=climax.exhaustion_min_gap,
        up_day_clusters=[(c.up_days, c.of_sessions) for c in climax.up_day_clusters],
        min_above_long_average=climax.min_above_long_average,
        fast_gain_min=climax.fast_gain_min,
        fast_gain_window=climax.fast_gain_window,
        fast_gain_min_move_sessions=climax.fast_gain_min_move_sessions,
    ).count


def _tranche2(
    trade: _Trade, n: int, s: SimSession, price: float, gate: GateInput, *, blocked: bool
) -> None:
    """Tranche 2 at the EOD review: a marketable limit buy, sized on its limit so the total
    risk stays within the plan (README §7.1)."""
    plan, risk = trade.plan, trade.risk_config
    if trade.order.tranches.second == 0 or plan.tranche2_filled or not s.tranche2_allowed:
        return
    passed = tranche2_gate(plan.strategy, gate, risk.plans)
    if tranche2_check(
        plan, gate=passed, session=n, blocked_by_plan=blocked, config=risk.guardrails
    ):
        return
    limit = round_up(price * (1 + risk.execution.marketable_limit_slippage))
    shares = tranche2_shares(
        planned_risk=trade.setup.entry.risk_amount,
        planned_shares=trade.order.tranches.total,
        filled_shares=trade.shares,
        fill_price=plan.entry_price,
        limit=limit,
        stop=plan.hard_stop,
    )
    fill = buy_fill(price, trade.slip)
    if shares == 0 or fill > limit:
        return
    average = (plan.entry_price * trade.shares + fill * shares) / (trade.shares + shares)
    trade.buy(n, FillKind.TRANCHE_2, shares, fill)
    trade.plan = with_tranche2(plan, entry_price=average)

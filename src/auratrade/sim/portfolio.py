"""The portfolio simulation (README §5.5, §15; `docs/backtests/portfolio-preregistration.md` §3;
task 3.20), pure: no I/O.

One account through daily closes, on the shared fill model (`sim/fills.py`) and the live pure
functions (`strategies/core_allocation.py`, `tax/israel.py`). Each session, in order:
1. **Payouts accrue** on the holdings (the market fund's dividends, the cash fund's interest);
   a margin loan (comparison C3) accrues interest.
2. **The stop** on the leveraged fund: a close at or below it sells the fund at that close,
   less slippage, and the proceeds wait in cash until the next E (§3.7).
3. **The halt's sale:** the session after the halt turns on, the leveraged fund is sold into
   the market fund (or, with margin, the loan repaid down to 1.0x) (§3.4).
4. **The rebalance on E:** to the targets decided on R, if they changed or a holding is more
   than ``drift_band`` from its target; sells first, then buys within the cash (§3.5). The
   leveraged fund's stop is set or raised to (1 - ``stop_fraction``) of E's price.
5. **Month end:** the accrued payouts are paid: income for the tax book, reinvested in the fund
   that paid them, as a new lot. **Year end:** the year's tax is paid from the cash fund first,
   then pro rata, without realizing a further gain (§3.8).
6. **The close:** equity marked; a margin call (C3) sells to 1.0x; the halt checked.
7. **On R:** the trend, the volatility cap and the halt decide the targets for E.

At the end everything is sold, less slippage, and the last year's tax paid: the final wealth.
Units are dollars' worth of each fund's price index, not whole shares (pre-registration §3.5).
The invariants of the pre-registration §7 raise `PortfolioInvariantError`.

**The live book** (task 4.3, #166) runs on the same steps with options that are all off in
the backtest. ``book`` gives the targets the Portfolio Manager set for each E (satellite funds
included, their prices in the market; an E without targets takes the rules'), and trades
each holding only if its own target changed or it drifted past the band (README §7.1, as the
order layer does; the hold rule of #163 relies on it), where the rules' runs rebalance the
whole account when any target changed.
``sell_at_end=False`` stops at T's close without the final sale, leaving the account in
`PortfolioRun.end`; ``rank_at_end`` also ranks T when it is an R whose E is past the market
(the `allocate` job's evening), so its ranking is the run's last. The twin is the same run
without ``book``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from auratrade.calendar.rebalance import rebalances
from auratrade.config import PortfolioBacktestConfig, PortfolioConfig
from auratrade.sim.fills import buy_price, sell_price
from auratrade.strategies.core_allocation import (
    WEIGHT_TOLERANCE,
    Halt,
    Trend,
    buffered_state,
    core_weights,
    exposure,
    halt_at_close,
    halt_on_ranking,
    realized_volatility,
    rule_level,
    sma,
    start_halt,
    trend_state,
    vol_capped,
)
from auratrade.tax.israel import UNITS_TOLERANCE, TaxBook, YearTax

CASH = "CASH"
"""Uninvested cash; negative under a margin loan (comparison C3)."""
START_PRICE = 100.0
LOAN_TOLERANCE = 0.01
"""Cash below minus a cent is a loan; above it, float noise from buying with all the cash."""


class PortfolioInvariantError(AssertionError):
    """A pre-registration §7 invariant failed: a bug, never a result."""


class TrendRule(StrEnum):
    MONTHLY_SMA = "MONTHLY_SMA"
    """The primary: month-end values against their 10-month SMA."""
    DAILY_SMA = "DAILY_SMA"
    """Comparison C1: the index on R against its ``sma_sessions``-session SMA."""
    NONE = "NONE"
    """A constant level: the benchmark, and diagnostic D-b."""


class LeverageMode(StrEnum):
    FUND = "FUND"
    """Leverage through the 2x fund (the primary)."""
    MARGIN = "MARGIN"
    """Leverage by borrowing (comparison C3)."""


@dataclass(frozen=True, slots=True)
class Rules:
    trend: TrendRule = TrendRule.MONTHLY_SMA
    leverage: LeverageMode = LeverageMode.FUND
    stop: bool = True
    vol_cap: bool = True
    halt: bool = True
    constant_level: float = 1.0
    sma_sessions: int = 200


BENCHMARK = Rules(trend=TrendRule.NONE, stop=False, vol_cap=False, halt=False)
"""The market fund bought at the first E and held (pre-registration §3.9)."""


@dataclass(frozen=True, slots=True)
class Market:
    days: list[date]
    prices: dict[str, list[float]]
    """Each fund's price index per session."""
    payouts: dict[str, list[float]]
    """Each fund's distribution per session, as a fraction of its value."""
    index: list[float]
    """The market's total-return index: the trend's signal."""
    returns: list[float]
    """The market fund's daily total returns: the volatility cap's input."""
    rf: list[float]
    """The T-bill return per session: the margin loan's base rate."""
    per_year: list[int]
    """Sessions in each session's calendar year (per-year rates are spread over them)."""


def synthesize(
    days: Sequence[date],
    market: Sequence[float],
    rf: Sequence[float],
    dividend_yield: Sequence[float],
    cfg: PortfolioConfig,
) -> Market:
    """The funds of pre-registration §3.2 from the market's daily total returns, the T-bill
    returns and the dividend yield (a year) in force each session.

    - The market fund: the total return less its fee, paid partly as dividends (their yield
      spread over the year's sessions), the rest in its price.
    - The leveraged fund: ``leverage`` x the total return, less financing of the borrowed part
      at the T-bill rate plus the spread, less its fee; no distributions.
    - The cash fund: the T-bill return less its fee, all paid as interest at a price of 100 (a
      negative net return lowers the price instead).
    """
    n = len(days)
    if not len(market) == len(rf) == len(dividend_yield) == n:
        raise ValueError("the series must be as long as the sessions")
    bt = cfg.backtest
    per_year = _sessions_per_year(days)
    mkt, lev, cash = cfg.market, cfg.leveraged, cfg.cash
    prices: dict[str, list[float]] = {mkt: [], lev: [], cash: []}
    payouts: dict[str, list[float]] = {mkt: [], lev: [], cash: []}
    index: list[float] = []
    returns: list[float] = []
    level = {mkt: START_PRICE, lev: START_PRICE, cash: START_PRICE}
    signal = START_PRICE
    for t in range(n):
        year = per_year[t]
        total = market[t] - bt.market_fee / year
        dividend = dividend_yield[t] / year
        lev_return = (
            cfg.leverage * market[t]
            - (cfg.leverage - 1) * (rf[t] + bt.financing_spread / year)
            - bt.leveraged_fee / year
        )
        interest = rf[t] - bt.cash_fee / year
        if total - dividend <= -1 or lev_return <= -1:
            raise ValueError(f"{days[t]}: a fund would lose its whole value")
        level[mkt] *= 1 + total - dividend
        level[lev] *= 1 + lev_return
        level[cash] *= 1 + min(interest, 0.0)
        for symbol, paid in ((mkt, dividend), (lev, 0.0), (cash, max(interest, 0.0))):
            prices[symbol].append(level[symbol])
            payouts[symbol].append(paid)
        signal *= 1 + total
        index.append(signal)
        returns.append(total)
    return Market(list(days), prices, payouts, index, returns, list(rf), per_year)


def _sessions_per_year(days: Sequence[date]) -> list[int]:
    counts: dict[int, int] = {}
    for d in days:
        counts[d.year] = counts.get(d.year, 0) + 1
    return [counts[d.year] for d in days]


@dataclass(frozen=True, slots=True)
class Trade:
    day: date
    symbol: str
    units: float
    """Positive for a buy, negative for a sale."""
    price: float
    """The fill, slippage included."""
    reason: str
    """REBALANCE, STOP, HALT, MARGIN_CALL, TAX or END."""


@dataclass(frozen=True, slots=True)
class Ranking:
    day: date
    """R."""
    state: Trend
    capped: bool
    halted: bool
    level: float
    flipped: bool


@dataclass(frozen=True, slots=True)
class Snapshot:
    """The account at one close (kept by runs that don't sell at the end)."""

    units: dict[str, float]
    cash: float
    """Uninvested cash with the payouts earned but not yet paid."""
    halted: bool
    peak: float
    tax_owed: float = 0.0
    """The estimated tax if everything were sold at this close (`TaxBook.tax_if_sold`)."""
    realized: float = 0.0
    """The year's realized gains so far."""


@dataclass(slots=True)
class AccountEnd:
    """The account at the last close of a run that didn't sell at the end."""

    units: dict[str, float]
    cash: float
    accrued: float
    """Payouts earned but not yet paid (they are paid at month end)."""
    tax: TaxBook
    halt: Halt
    stop: float | None
    targets: dict[str, float] | None
    """The targets of the last rebalance (None before the first)."""


@dataclass(slots=True)
class PortfolioRun:
    start_equity: float
    days: list[date] = field(default_factory=list)
    marks: list[float] = field(default_factory=list)
    """Equity at each close from the first R, after the year's tax is paid."""
    trades: list[Trade] = field(default_factory=list)
    rankings: list[Ranking] = field(default_factory=list)
    stopouts: list[date] = field(default_factory=list)
    halt_starts: list[date] = field(default_factory=list)
    margin_calls: list[date] = field(default_factory=list)
    taxes: list[YearTax] = field(default_factory=list)
    final_wealth: float = 0.0
    """After the final sale and its tax."""
    end: AccountEnd | None = None
    """Set instead of the final sale with ``sell_at_end=False``."""
    snapshots: list[Snapshot] = field(default_factory=list)
    """One per mark, with ``sell_at_end=False``."""

    @property
    def flips(self) -> int:
        return sum(r.flipped for r in self.rankings)

    @property
    def tax_paid(self) -> float:
        return math.fsum(t.total for t in self.taxes)


@dataclass(slots=True)
class _Account:
    units: dict[str, float]
    cash: float
    accrued: dict[str, float]
    tax: TaxBook

    def value(self, prices: Mapping[str, float]) -> float:
        held = math.fsum(u * prices[s] for s, u in self.units.items())
        return held + self.cash + math.fsum(self.accrued.values())


def simulate_portfolio(
    market: Market,
    rules: Rules,
    cfg: PortfolioConfig,
    *,
    start: int,
    end: int,
    equity: float,
    tax_rate: float,
    offset: int = 1,
    book: Mapping[date, Mapping[str, float]] | None = None,
    sell_at_end: bool = True,
    rank_at_end: bool = False,
) -> PortfolioRun:
    """The account from cash at the close of the first R at or after session ``start`` to its
    sale at the close of session ``end`` (or, with ``sell_at_end=False``, to that close)."""
    if not 0 <= start <= end < len(market.days) or (sell_at_end and start == end):
        raise ValueError("start and end must be sessions of the market, start first")
    if rank_at_end and sell_at_end:
        raise ValueError("rank_at_end needs sell_at_end=False")
    core = (cfg.market, cfg.leveraged, cfg.cash)
    if not set(core) <= market.prices.keys():
        raise ValueError(f"the market needs {core}")
    for day, targets in (book or {}).items():
        unknown = sorted(set(targets) - market.prices.keys())
        if unknown:
            raise ValueError(f"{day}: no prices for {unknown}")
    sessions = [_Session(d) for d in market.days]
    position = {d: i for i, d in enumerate(market.days)}
    schedule = [(position[r.ranking], position[r.execution]) for r in rebalances(sessions, offset)]
    by_ranking = dict(schedule)
    ranking_days = [r for r, _ in schedule]
    rank_end = rank_at_end and end not in by_ranking
    if rank_end:
        ranking_days = [r for r in ranking_days if r < end] + [end]
    first = next((r for r in ranking_days if r >= start), None)
    if first is None or first > end or (sell_at_end and first == end):
        raise ValueError("no ranking session between start and end")
    sim = _Simulation(market, rules, cfg, equity, tax_rate, ranking_days, book)
    sim.snapshots = not sell_at_end
    for i in range(first, end + 1):
        sim.session(i, first, end, by_ranking.get(i))
    if sell_at_end:
        sim.finish(end)
    else:
        if rank_end:
            sim.rank(end, sim.run.marks[-1])
        sim.keep(end)
    return sim.run


@dataclass(frozen=True, slots=True)
class _Session:
    day: date
    is_early_close: bool = False


class _Simulation:
    def __init__(
        self,
        market: Market,
        rules: Rules,
        cfg: PortfolioConfig,
        equity: float,
        tax_rate: float,
        ranking_days: list[int],
        book: Mapping[date, Mapping[str, float]] | None = None,
    ) -> None:
        self.m = market
        self.rules = rules
        self.cfg = cfg
        self.bt: PortfolioBacktestConfig = cfg.backtest
        core = (cfg.market, cfg.leveraged, cfg.cash)
        self.funds = (*core, *sorted(set(market.prices) - set(core)))
        # A satellite fund trades at the market fund's slippage (all are large, liquid ETFs).
        self.slip = dict.fromkeys(self.funds, self.bt.market_slippage)
        self.slip.update(
            {
                cfg.leveraged: self.bt.leveraged_slippage,
                cfg.cash: self.bt.cash_slippage,
            }
        )
        self.book = book
        self.snapshots = False
        self.acct = _Account({}, equity, {}, TaxBook(tax_rate))
        self.run = PortfolioRun(equity)
        self.ranking_days = ranking_days
        self.state: Trend | None = None
        self.halt: Halt = start_halt(equity)
        self.halt_sale = False
        self.stop: float | None = None
        self.pending: tuple[int, dict[str, float], Ranking] | None = None
        self.targets: dict[str, float] | None = None

    # --- one session --------------------------------------------------------------------

    def session(self, i: int, first: int, end: int, execution: int | None) -> None:
        day = self.m.days[i]
        prices = self._prices(i)
        if i > first:
            self._accrue(i, prices)
            self._stop(day, prices)
            self._halt_sale(day, prices)
        if self.pending is not None and self.pending[0] == i:
            self._rebalance(day, prices, self.pending[1], self.pending[2])
            self.pending = None
        last = i == end
        if not last and self._month_end(i):
            self._pay_out(day, prices)
        if not last and self._year_end(i):
            self._pay_tax(day, prices)
        value = self.acct.value(prices)
        if self.rules.leverage is LeverageMode.MARGIN:
            value = self._margin_call(day, prices, value)
        if self.rules.halt:
            self.halt, turned = halt_at_close(self.halt, value, self.cfg)
            if turned:
                self.halt_sale = True
                self.run.halt_starts.append(day)
        if execution is not None and execution <= end:
            self._decide(i, execution, value)
        self.run.days.append(day)
        self.run.marks.append(value)
        if self.snapshots:
            a = self.acct
            accrued = math.fsum(a.accrued.values())
            # Every lot's fund: a sale's float dust can leave a lot whose units are gone.
            values = {s: a.units.get(s, 0.0) * prices[s] for s in a.tax.symbols()}
            owed = a.tax.tax_if_sold(day.year, values, accrued)
            self.run.snapshots.append(
                Snapshot(
                    dict(a.units),
                    a.cash + accrued,
                    self.halt.on,
                    self.halt.peak,
                    owed,
                    a.tax.realized(day.year),
                )
            )

    def finish(self, end: int) -> None:
        day = self.m.days[end]
        prices = self._prices(end)
        for symbol in sorted(self.acct.units):
            self._sell(day, symbol, self.acct.units[symbol], prices, "END")
        # Only positive payouts accrue (`_accrue`).
        for amount in self.acct.accrued.values():
            self.acct.tax.add_income(day, amount)
            self.acct.cash += amount
        self.acct.accrued.clear()
        tax = self.acct.tax.close_year(day.year)
        self.run.taxes.append(tax)
        self.run.final_wealth = self.acct.cash - tax.total

    def keep(self, end: int) -> None:
        """Stop at the close of ``end``: the account as it stands."""
        a = self.acct
        self.run.end = AccountEnd(
            dict(a.units),
            a.cash,
            math.fsum(a.accrued.values()),
            a.tax,
            self.halt,
            self.stop,
            None if self.targets is None else dict(self.targets),
        )

    # --- steps --------------------------------------------------------------------------

    def _prices(self, i: int) -> dict[str, float]:
        return {s: self.m.prices[s][i] for s in self.funds}

    def _accrue(self, i: int, prices: Mapping[str, float]) -> None:
        for symbol, units in self.acct.units.items():
            paid = units * prices[symbol] * self.m.payouts[symbol][i]
            if paid > 0:
                self.acct.accrued[symbol] = self.acct.accrued.get(symbol, 0.0) + paid
        if self.acct.cash < -LOAN_TOLERANCE:
            rate = self.m.rf[i] + self.bt.margin_spread / self.m.per_year[i]
            self.acct.cash *= 1 + rate

    def _stop(self, day: date, prices: Mapping[str, float]) -> None:
        lev = self.cfg.leveraged
        if self.stop is None or prices[lev] > self.stop or not self.acct.units.get(lev):
            return
        proceeds = self._sell(day, lev, self.acct.units[lev], prices, "STOP")
        self._buy(day, self.cfg.cash, proceeds, prices, "STOP")
        self.stop = None
        self.run.stopouts.append(day)

    def _halt_sale(self, day: date, prices: Mapping[str, float]) -> None:
        if not self.halt_sale:
            return
        self.halt_sale = False
        lev, mkt = self.cfg.leveraged, self.cfg.market
        if self.acct.units.get(lev):
            proceeds = self._sell(day, lev, self.acct.units[lev], prices, "HALT")
            self._buy(day, mkt, proceeds, prices, "HALT")
            self.stop = None
        elif self.acct.cash < -LOAN_TOLERANCE:
            self._delever(day, prices, "HALT")

    def _rebalance(
        self, day: date, prices: Mapping[str, float], targets: dict[str, float], ranking: Ranking
    ) -> None:
        value = self.acct.value(prices)
        held = {s: u * prices[s] / value for s, u in self.acct.units.items()}
        changed = self.targets is None or _differs(targets, self.targets)
        drifted = any(
            abs(held.get(s, 0.0) - targets.get(s, 0.0)) > self.cfg.drift_band
            for s in set(held) | set(targets)
            if s != CASH
        )
        previous = self.targets
        self.targets = targets
        if self.book is not None and previous is not None:
            # Each holding on its own: its target changed, or it drifted past the band.
            moved = {
                s
                for s in self.funds
                if abs(targets.get(s, 0.0) - previous.get(s, 0.0)) > WEIGHT_TOLERANCE
                or abs(held.get(s, 0.0) - targets.get(s, 0.0)) > self.cfg.drift_band
            }
            if moved:
                self._trade_to(day, prices, targets, value, moved)
        elif changed or drifted:
            self._trade_to(day, prices, targets, value)
        self._check_after_rebalance(targets, ranking)
        lev = self.cfg.leveraged
        if self.rules.stop and self.acct.units.get(lev):
            new = prices[lev] * (1 - self.cfg.stop_fraction)
            self.stop = new if self.stop is None else max(self.stop, new)
        elif not self.acct.units.get(lev):
            self.stop = None

    def _trade_to(
        self,
        day: date,
        prices: Mapping[str, float],
        targets: dict[str, float],
        value: float,
        only: set[str] | None = None,
    ) -> None:
        funds = [s for s in self.funds if only is None or s in only]
        want = {s: targets.get(s, 0.0) * value for s in funds}
        for symbol in funds:
            units = self.acct.units.get(symbol, 0.0)
            excess = units * prices[symbol] - want[symbol]
            if units and excess > WEIGHT_TOLERANCE * value:
                sold = units if want[symbol] <= 0 else excess / prices[symbol]
                self._sell(day, symbol, sold, prices, "REBALANCE")
        short = {
            s: want[s] - self.acct.units.get(s, 0.0) * prices[s]
            for s in funds
            if want[s] - self.acct.units.get(s, 0.0) * prices[s] > WEIGHT_TOLERANCE * value
        }
        floor = targets.get(CASH, 0.0) * value if targets.get(CASH, 0.0) < 0 else 0.0
        room = self.acct.cash - floor
        needed = math.fsum(short.values())
        scale = min(1.0, room / needed) if needed > 0 else 0.0
        for symbol in sorted(short):
            self._buy(day, symbol, short[symbol] * scale, prices, "REBALANCE")

    def _pay_out(self, day: date, prices: Mapping[str, float]) -> None:
        for symbol, amount in sorted(self.acct.accrued.items()):
            self.acct.tax.add_income(day, amount)
            units = amount / prices[symbol]
            self.acct.units[symbol] = self.acct.units.get(symbol, 0.0) + units
            self.acct.tax.buy(symbol, day, units, amount)
        self.acct.accrued.clear()

    def _pay_tax(self, day: date, prices: Mapping[str, float]) -> None:
        tax = self.acct.tax.close_year(day.year)
        self.run.taxes.append(tax)
        owed = tax.total
        if owed <= 0:
            return
        cash = self.cfg.cash
        if self.acct.units.get(cash):
            owed -= self._withdraw(cash, min(owed, self.acct.units[cash] * prices[cash]), prices)
        values = {s: u * prices[s] for s, u in self.acct.units.items()}
        total = math.fsum(values.values())
        if owed > 0 and total > 0:
            for symbol in sorted(values):
                self._withdraw(symbol, owed * values[symbol] / total, prices)
            owed = 0.0
        self.acct.cash -= owed

    def _withdraw(self, symbol: str, amount: float, prices: Mapping[str, float]) -> float:
        units = min(amount / prices[symbol], self.acct.units[symbol])
        self.acct.tax.withdraw(symbol, units)
        self._reduce(symbol, units)
        return units * prices[symbol]

    def _margin_call(self, day: date, prices: Mapping[str, float], value: float) -> float:
        held = self.acct.units.get(self.cfg.market, 0.0) * prices[self.cfg.market]
        if self.acct.cash < -LOAN_TOLERANCE and value < self.bt.margin_maintenance * held:
            self._delever(day, prices, "MARGIN_CALL")
            self.run.margin_calls.append(day)
            return self.acct.value(prices)
        return value

    def _delever(self, day: date, prices: Mapping[str, float], reason: str) -> None:
        """Sell the market fund until the loan is repaid (1.0x)."""
        mkt = self.cfg.market
        repay = -self.acct.cash / (1 - self.slip[mkt])
        # A loan is only ever taken to hold more of the market fund, so some is held.
        self._sell(day, mkt, min(repay / prices[mkt], self.acct.units[mkt]), prices, reason)

    # --- the decision on R --------------------------------------------------------------

    def _decide(self, i: int, execution: int, value: float) -> None:
        ranking = self.rank(i, value)
        self.pending = (execution, self._book_targets(execution, ranking.level), ranking)

    def rank(self, i: int, value: float) -> Ranking:
        """R's trend, cap and halt, and the rule level."""
        cfg, rules = self.cfg, self.rules
        previous = self.state
        if rules.trend is TrendRule.NONE:
            state = Trend.UP
        elif rules.trend is TrendRule.MONTHLY_SMA:
            month_ends = [self.m.index[r] for r in self.ranking_days if r <= i]
            found = trend_state(previous, month_ends, cfg)
            if found is None:
                raise ValueError(f"{self.m.days[i]}: too little history for the trend")
            state = found
        else:
            window = self.m.index[max(0, i + 1 - rules.sma_sessions) : i + 1]
            average = sma(window, rules.sma_sessions)
            if average is None:
                raise ValueError(f"{self.m.days[i]}: too little history for the trend")
            state = buffered_state(previous, self.m.index[i], average, cfg)
        flipped_up = previous is Trend.DOWN and state is Trend.UP
        if rules.halt:
            self.halt = halt_on_ranking(self.halt, flipped_up, value)
        halted = rules.halt and self.halt.on
        capped = False
        if rules.vol_cap:
            window = self.m.returns[max(0, i + 1 - cfg.vol_sessions) : i + 1]
            capped = vol_capped(realized_volatility(window, cfg), cfg)
        if rules.trend is TrendRule.NONE:
            level = rules.constant_level
            if capped or halted:
                level = min(level, cfg.capped_level)
        else:
            level = rule_level(state, capped, halted, cfg)
        self.state = state
        flipped = previous is not None and previous != state
        ranking = Ranking(self.m.days[i], state, capped, halted, level, flipped)
        self.run.rankings.append(ranking)
        return ranking

    def _book_targets(self, execution: int, level: float) -> dict[str, float]:
        """The rules' targets, or with a book its targets for E. An E the book has no targets
        for (the Portfolio Manager never ran) takes the rules': they always fit the limits."""
        chosen = None if self.book is None else self.book.get(self.m.days[execution])
        if chosen is None:
            return self._targets(level)
        return {s: w for s, w in chosen.items() if w > WEIGHT_TOLERANCE}

    def _targets(self, level: float) -> dict[str, float]:
        if self.rules.leverage is LeverageMode.FUND or level <= 1:
            return core_weights(level, self.cfg)
        return {self.cfg.market: level, CASH: 1 - level}

    # --- trades and checks --------------------------------------------------------------

    def _buy(
        self, day: date, symbol: str, dollars: float, prices: Mapping[str, float], reason: str
    ) -> None:
        if dollars <= 0:
            return
        fill = buy_price(prices[symbol], self.slip[symbol])
        units = dollars / fill
        self.acct.units[symbol] = self.acct.units.get(symbol, 0.0) + units
        self.acct.cash -= dollars
        self.acct.tax.buy(symbol, day, units, dollars)
        self.run.trades.append(Trade(day, symbol, units, fill, reason))

    def _sell(
        self, day: date, symbol: str, units: float, prices: Mapping[str, float], reason: str
    ) -> float:
        units = min(units, self.acct.units[symbol])
        fill = sell_price(prices[symbol], self.slip[symbol])
        proceeds = units * fill
        self.acct.tax.sell(symbol, day, units, proceeds)
        self._reduce(symbol, units)
        self.acct.cash += proceeds
        self.run.trades.append(Trade(day, symbol, -units, fill, reason))
        return proceeds

    def _reduce(self, symbol: str, units: float) -> None:
        left = self.acct.units[symbol] - units
        if left <= UNITS_TOLERANCE * max(1.0, self.acct.units[symbol]):
            del self.acct.units[symbol]
        else:
            self.acct.units[symbol] = left

    def _check_after_rebalance(self, targets: dict[str, float], ranking: Ranking) -> None:
        cfg = self.cfg
        if self.rules.leverage is LeverageMode.FUND:
            level = exposure(targets, cfg)
        else:
            level = targets.get(cfg.market, 0.0)
        ceiling = cfg.capped_level if ranking.capped or ranking.halted else cfg.max_level
        if level > ceiling + 1e-9:
            raise PortfolioInvariantError(f"{ranking.day}: exposure {level} above {ceiling}")
        for symbol, units in self.acct.units.items():
            if units < 0:
                raise PortfolioInvariantError(f"{ranking.day}: negative {symbol}")
            lots = self.acct.tax.units(symbol)
            if not math.isclose(lots, units, rel_tol=1e-6, abs_tol=1e-6):
                raise PortfolioInvariantError(f"{ranking.day}: {symbol} lots {lots} != {units}")
        if self.acct.units.get(cfg.leveraged) and not targets.get(cfg.leveraged):
            raise PortfolioInvariantError(f"{ranking.day}: the leveraged fund held below 1x")
        if self.acct.cash < -LOAN_TOLERANCE and self.rules.leverage is LeverageMode.FUND:
            raise PortfolioInvariantError(f"{ranking.day}: borrowed without margin")

    def _month_end(self, i: int) -> bool:
        days = self.m.days
        return i + 1 >= len(days) or days[i + 1].month != days[i].month

    def _year_end(self, i: int) -> bool:
        days = self.m.days
        return i + 1 >= len(days) or days[i + 1].year != days[i].year


def _differs(a: Mapping[str, float], b: Mapping[str, float]) -> bool:
    return any(abs(a.get(k, 0.0) - b.get(k, 0.0)) > WEIGHT_TOLERANCE for k in set(a) | set(b))

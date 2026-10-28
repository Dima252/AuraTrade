"""The portfolio's trades (README §5.6.3, §7; task 5.1), on an order section
(`execution.runtime`), so under the single-writer lock and after the stop invariant.

**The rebalance** (on E at 15:40, and on its retry while E's run didn't finish):
1. Clear what an earlier run left working in the funds (unfilled buys, sales): each run decides
   afresh from the broker. The leveraged fund is then protected at its level, which keeps the
   filled part of a partly filled buy covered (#115).
2. Plan from the broker's holdings and equity and the decision prices (`execution.rebalance`);
   buys that may not go in (the kill switch, the halt, a mismatch, ...) are held back.
3. The leveraged fund's stop level for this E is recorded before any of its orders.
4. Sells first: the leveraged fund through the sell sequence (reduce the stop, sell, restore),
   the others plainly.
5. Buys within the cash the sales left, at marketable limits; the leveraged fund as an OTO
   whose GTC stop leg rests at the level.
6. The stop raise: the leveraged fund's stops to the new level, never down.

**The halt's sale** (any session while the drawdown halt is on and the leveraged fund is held):
all of it through the sell sequence, then the market fund bought with the proceeds. Neither the
kill switch nor any other block stops it: it lowers exposure (README §5.9).

**The stop's proceeds** (any session after the rebalance finished, until the next E): when the
leveraged fund's stop filled since then, the proceeds go into the cash fund (README §7.2), as far
as the cash left pays for whole shares; the buy follows the same blocks as the rebalance's (under
the kill switch only if the cash fund is already held). What earlier runs bought of the cash
fund since the fill counts against the proceeds, so a later run buys only what is still owed.

In dry_run nothing is sent: the plan is reported.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, select, update

from auratrade.calendar.sessions import MarketCalendar
from auratrade.config import PortfolioConfig
from auratrade.db.flags import read_flags
from auratrade.db.models import FundStop, PortfolioTarget
from auratrade.execution import funds, ids, store
from auratrade.execution.broker import Broker, BrokerOrder, Side
from auratrade.execution.orders import BuyResult, SaleResult, SaleStatus
from auratrade.execution.prices import marketable_buy_limit
from auratrade.execution.rebalance import (
    Action,
    Blocks,
    Move,
    allow_buys,
    buy_block,
    fit_buys,
    halted_targets,
    leveraged_stop,
    plan,
)
from auratrade.execution.runtime import InvariantReport, Section
from auratrade.strategies.core_allocation import WEIGHT_TOLERANCE

REBALANCE = "REBALANCE"
HALT = "HALT"
NY = ZoneInfo("America/New_York")
# A stop placed a little before its ENTRY row's session (a broker timestamp) is still found.
STOP_LOOKBACK_MARGIN = timedelta(days=1)
# Orders are listed by creation after a time: a buy placed in the second of the fill counts.
FILL_MARGIN = timedelta(seconds=1)


@dataclass(frozen=True)
class Due:
    """A rebalance whose targets are still to be traded."""

    targets_id: int
    execution: date
    """E."""
    targets: dict[str, float]
    previous: dict[str, float] | None
    """The rebalance before it: a holding whose target changed from these is traded."""
    retry: int
    """0 on E, then the sessions since it."""


def due_rebalance(
    conn: Connection, calendar: MarketCalendar, session: date, cfg: PortfolioConfig
) -> Due | None:
    """The latest decided rebalance with E on or before `session`, unless it finished or its
    retries (``retry_sessions``) have passed."""
    found = store.entities(
        conn,
        select(PortfolioTarget)
        .where(
            PortfolioTarget.kind == "REBALANCE",
            PortfolioTarget.status == "DECIDED",
            PortfolioTarget.execution_session <= session,
        )
        .order_by(PortfolioTarget.execution_session.desc())
        .limit(1),
    )
    row = found[0] if found else None
    if row is None or row.executed_at is not None or row.targets is None:
        return None
    retry = len(calendar.sessions_between(row.execution_session, session)) - 1
    if retry > cfg.retry_sessions:
        return None
    previous = conn.scalar(
        select(PortfolioTarget.targets)
        .where(
            PortfolioTarget.kind == "REBALANCE",
            PortfolioTarget.status == "DECIDED",
            PortfolioTarget.execution_session < row.execution_session,
        )
        .order_by(PortfolioTarget.execution_session.desc())
        .limit(1)
    )
    return Due(
        targets_id=row.id,
        execution=row.execution_session,
        targets={str(k): float(v) for k, v in row.targets.items()},
        previous=None if previous is None else {str(k): float(v) for k, v in previous.items()},
        retry=retry,
    )


def record_execution(
    conn: Connection, due: Due, entry: Mapping[str, Any], *, finished: bool, at: Any
) -> None:
    current = conn.scalar(
        select(PortfolioTarget.executions).where(PortfolioTarget.id == due.targets_id)
    )
    values: dict[str, Any] = {"executions": [*(current or []), dict(entry)]}
    if finished:
        values["executed_at"] = at
    conn.execute(update(PortfolioTarget).where(PortfolioTarget.id == due.targets_id).values(values))


def increase_blocks(section: Section, invariant: InvariantReport) -> list[str]:
    """Why no exposure may be added this run (fail closed, CLAUDE.md §3.6). Sales go ahead."""
    reasons: list[str] = []
    ctx = section.ctx
    with section.deps.engine.connect() as conn:
        flags = read_flags(conn)
        mismatches = store.open_mismatches(conn)
    if not flags.trading_enabled:
        reasons.append("kill switch on")
    session = section.calendar.session_for(ctx.session_date)
    if session is None:
        reasons.append("not a session")
    else:
        if not session.calendar_agrees:
            reasons.append(f"calendar cross-check {session.calendar_check}")
        previous = section.calendar.previous_session(ctx.session_date)
        if previous.data_status != "COMPLETE":
            reasons.append(f"{previous.day} data {previous.data_status}")
    if any(symbol is None for _, symbol in mismatches):
        reasons.append("an account-level mismatch is open")
    if not invariant.clean:
        reasons.append("the stop invariant needed repair")
    if section.rec.unknown:
        reasons.append(f"unknown positions at the broker: {section.rec.unknown}")
    return reasons


def holdings(broker: Broker, names: Collection[str]) -> dict[str, int]:
    return {p.symbol: p.qty for p in broker.positions() if p.symbol in names and p.qty > 0}


def _clear(section: Section, cfg: PortfolioConfig, level: float | None) -> None:
    """Cancel every working order in the funds except the stops: a crashed run's buys and
    sales. The leveraged fund is then protected at its level."""
    desk, names = section.desk, funds.funds(cfg)
    for order in section.deps.broker.open_orders():
        if order.symbol in names and not order.is_sell_stop:
            desk.cancel_once(order)
            desk.actions.append(
                {"action": "cancel", "symbol": order.symbol, "order_id": order.id, "why": "stale"}
            )
    if level is not None and desk.state(cfg.leveraged)[0] > 0:
        desk.protect(cfg.leveraged, level)


def _sale(result: SaleResult) -> dict[str, Any]:
    return {"requested": result.requested, "sold": result.sold, "status": result.status.value}


def _bought(result: BuyResult) -> dict[str, Any]:
    return {
        "requested": result.requested,
        "bought": result.bought,
        "price": result.price,
        "failed": result.failed,
    }


def _move(move: Move) -> dict[str, Any]:
    out = asdict(move)
    out["action"] = move.action.value
    out["why"] = move.why.value
    return out


def rebalance(section: Section, invariant: InvariantReport, due: Due) -> dict[str, Any]:
    """Trade toward `due`'s targets; `finished` when nothing is owed."""
    ctx, deps, desk = section.ctx, section.deps, section.desk
    cfg = ctx.risk.portfolio
    lev = cfg.leveraged
    with deps.engine.connect() as conn:
        halted = read_flags(conn).halt_drawdown
        current = funds.stop_level(conn, lev)
    blocks = Blocks(
        increases=tuple(increase_blocks(section, invariant)),
        halted=halted,
        mismatched=frozenset(desk.blocked_sales),
    )
    targets = halted_targets(due.targets, cfg) if halted else due.targets
    out: dict[str, Any] = {
        "execution": due.execution.isoformat(),
        "retry": due.retry,
        "blocks": list(blocks.increases),
        "halted": halted,
    }
    if not desk.market_open:
        return {**out, "finished": False, "skipped": "market closed"}

    held_level = None if current is None else current.level
    if not section.dry_run:
        _clear(section, cfg, held_level)
    held = holdings(deps.broker, funds.funds(cfg))
    equity = deps.broker.account().equity
    prices = deps.prices.prices(sorted(set(held) | set(targets)))
    slip = ctx.risk.execution.marketable_limit_slippage
    moves = allow_buys(plan(targets, due.previous, held, prices, equity, cfg, slip), blocks, cfg)

    lev_price = prices.get(lev)
    lev_held = held.get(lev, 0) > 0
    level = held_level if lev_held else None
    if lev_price is not None and targets.get(lev, 0.0) > WEIGHT_TOLERANCE:
        level = leveraged_stop(lev_price, level, cfg)
    out |= {"equity": equity, "level": level, "planned": [_move(m) for m in moves]}
    if section.dry_run:
        return {**out, "finished": False, "dry_run": True}

    buying_lev = any(m.symbol == lev and m.action is Action.BUY for m in moves)
    if level is not None and (lev_held or buying_lev) and level != held_level:
        with deps.engine.begin() as conn:
            level = funds.write_stop_level(
                conn,
                lev,
                level,
                session=ctx.session_date,
                change="RAISE" if lev_held else "ENTRY",
                price=lev_price,
                details={"execution": due.execution.isoformat()},
            )

    finished = not any(m.owed for m in moves)
    sales: dict[str, dict[str, Any]] = {}
    for move in (m for m in moves if m.action is Action.SELL):
        if move.symbol == lev:
            # A sell move always has its price; a fund held without a recorded level was
            # bought outside the order layer: restore where a buy today would put it.
            restore = level if level is not None else leveraged_stop(move.price or 0.0, None, cfg)
            result = desk.sell(lev, move.qty, REBALANCE, restore)
        else:
            result = desk.sell_plain(move.symbol, move.qty, REBALANCE)
        sales[move.symbol] = _sale(result)
        finished &= result.status in (SaleStatus.FILLED, SaleStatus.NOTHING)

    cash = deps.broker.account().cash
    buys: dict[str, dict[str, Any]] = {}
    fitted = fit_buys(moves, cash)
    for move in (m for m in fitted if m.action is Action.BUY):
        stop = level if move.symbol == lev else None
        bought = desk.buy(move.symbol, move.qty, move.limit or 0.0, stop=stop)
        buys[move.symbol] = _bought(bought)
        finished &= bought.owed == 0

    if level is not None and desk.state(lev)[0] > 0:
        desk.protect(lev, level)
    return {
        **out,
        "finished": finished,
        "fitted": [_move(m) for m in fitted if m.action is not Action.SELL],
        "sales": sales,
        "buys": buys,
    }


def halt_sale(section: Section) -> dict[str, Any] | None:
    """The drawdown halt's sale (README §7.3): the leveraged fund into the market fund. None
    when there is nothing to sell."""
    ctx, deps, desk = section.ctx, section.deps, section.desk
    cfg = ctx.risk.portfolio
    lev, market = cfg.leveraged, cfg.market
    qty = holdings(deps.broker, [lev]).get(lev, 0)
    if qty <= 0:
        return None
    prices = deps.prices.prices([lev, market])
    with deps.engine.connect() as conn:
        current = funds.stop_level(conn, lev)
    lev_price, market_price = prices[lev], prices[market]
    out: dict[str, Any] = {"symbol": lev, "qty": qty}
    if lev_price is None:
        desk.alert(f"no price for {lev}: the drawdown halt's sale of {qty} waits")
        return {**out, "skipped": "no price"}
    # Held without a recorded level (bought outside the order layer): where a buy today would
    # put it, so a failed sale restores a stop.
    level = current.level if current is not None else leveraged_stop(lev_price, None, cfg)
    out["level"] = level
    if not desk.market_open:
        desk.alert(f"the drawdown halt's sale of {qty} {lev} waits for the market")
        return {**out, "skipped": "market closed"}
    if section.dry_run:
        return {**out, "dry_run": True}
    sale = desk.sell(lev, qty, HALT, level)
    out["sale"] = _sale(sale)
    if sale.owed:
        desk.alert(f"the drawdown halt's sale: {sale.sold} of {qty} {lev} sold; the rest is owed")
    if sale.sold <= 0:
        return out
    if market_price is None:
        desk.alert(f"no price for {market}: the halt's proceeds stay in cash")
        return out
    if market in desk.blocked_sales:
        desk.alert(f"the halt's proceeds stay in cash: an open mismatch in {market}")
        return out
    limit = marketable_buy_limit(market_price, ctx.risk.execution.marketable_limit_slippage)
    spend = min(sale.sold * lev_price, deps.broker.account().cash)
    shares = math.floor(spend / limit) if spend > 0 else 0
    if shares > 0:
        out["buy"] = _bought(desk.buy(market, shares, limit, stop=None))
    return out


def last_finished(conn: Connection) -> datetime | None:
    """When the latest decided rebalance finished; None while it is owed (or before any)."""
    row = conn.execute(
        select(PortfolioTarget.executed_at)
        .where(PortfolioTarget.kind == "REBALANCE", PortfolioTarget.status == "DECIDED")
        .order_by(PortfolioTarget.execution_session.desc())
        .limit(1)
    ).first()
    return None if row is None else row.executed_at


def bought_from_flat(conn: Connection, symbol: str) -> date | None:
    """The session the fund was last bought from flat (its ENTRY level): no stop of the
    holding was placed before it."""
    day: date | None = conn.scalar(
        select(FundStop.session_date)
        .where(FundStop.symbol == symbol, FundStop.change == "ENTRY")
        .order_by(FundStop.id.desc())
        .limit(1)
    )
    return day


def stopped_out(order: BrokerOrder) -> bool:
    """A sale by the stop: the stop order itself, or the sale at once the invariant makes when
    the price is already through the level (`OrderDesk.protect`)."""
    if order.side is not Side.SELL:
        return False
    if order.is_sell_stop:
        return True
    parsed = ids.parse(order.client_order_id)
    return parsed is not None and parsed.purpose is ids.Purpose.EXIT and parsed.detail == "STOP"


def stop_proceeds(section: Section, invariant: InvariantReport) -> dict[str, Any] | None:
    """The leveraged fund's stop fills since the rebalance finished, into the cash fund (README
    §7.2). None when no stop filled."""
    ctx, deps, desk = section.ctx, section.deps, section.desk
    cfg = ctx.risk.portfolio
    lev, cash_fund = cfg.leveraged, cfg.cash
    with deps.engine.connect() as conn:
        since = last_finished(conn)
        entry = bought_from_flat(conn, lev)
    if since is None or entry is None:
        return None
    # Orders are listed by creation: the stop that filled may rest from an earlier E.
    created_after = datetime.combine(entry, time(0), tzinfo=NY) - STOP_LOOKBACK_MARGIN
    fills = [
        o
        for o in deps.broker.orders_since(created_after, [lev])
        if stopped_out(o) and o.filled_qty > 0 and o.filled_at is not None and o.filled_at >= since
    ]
    if not fills:
        return None
    proceeds = math.fsum(o.filled_qty * (o.filled_avg_price or 0.0) for o in fills)
    first_fill = min(o.filled_at for o in fills if o.filled_at is not None)
    # What earlier runs already put in: only the proceeds go in, never the rebalance's change.
    bought = math.fsum(
        o.filled_qty * (o.filled_avg_price or 0.0)
        for o in deps.broker.orders_since(first_fill - FILL_MARGIN, [cash_fund])
        if o.side is Side.BUY and o.filled_qty > 0
    )
    out: dict[str, Any] = {
        "symbol": lev,
        "stopped": sum(o.filled_qty for o in fills),
        "proceeds": round(proceeds, 2),
        "already_in": round(bought, 2),
        "into": cash_fund,
    }
    if bought == 0:
        # The first run since the fill (README §5.9): the operator raises the event by hand.
        desk.alert(
            f"{lev}'s stop filled: {out['stopped']} shares sold for {proceeds:,.2f}; the "
            f"proceeds go into {cash_fund}. Explain it with portfolio_manager --event STOP_FILLED."
        )
    held = holdings(deps.broker, [cash_fund]).get(cash_fund, 0)
    blocks = Blocks(
        increases=tuple(increase_blocks(section, invariant)),
        mismatched=frozenset(desk.blocked_sales),
    )
    block = buy_block(cash_fund, held, blocks, cfg)
    if block is not None:
        desk.alert(f"the stop's proceeds stay in cash: {block}")
        return {**out, "skipped": block}
    if not desk.market_open:
        return {**out, "skipped": "market closed"}
    price = deps.prices.prices([cash_fund])[cash_fund]
    if price is None:
        desk.alert(f"no price for {cash_fund}: the stop's proceeds stay in cash")
        return {**out, "skipped": "no price"}
    limit = marketable_buy_limit(price, ctx.risk.execution.marketable_limit_slippage)
    spend = min(proceeds - bought, deps.broker.account().cash)
    shares = math.floor(spend / limit) if spend > 0 else 0
    out |= {"shares": shares, "limit": limit}
    if shares <= 0:
        return out
    if section.dry_run:
        return {**out, "dry_run": True}
    out["buy"] = _bought(desk.buy(cash_fund, shares, limit, stop=None))
    return out

"""Spike 0.3b: Alpaca order behaviour on the paper endpoint.

Every order carries a `spk-` client_order_id so `cleanup` can find it. Events go to
spikes/alpaca_orders/output/*.jsonl (git-ignored); docs/spikes/alpaca-orders.md has the findings.

    uv run python -m spikes.alpaca_orders account            # any time
    uv run python -m spikes.alpaca_orders independence       # any time (dev + paper accounts)
    uv run python -m spikes.alpaca_orders dedupe             # any time
    uv run python -m spikes.alpaca_orders oto_structure      # any time (orders rest, never fill)
    uv run python -m spikes.alpaca_orders opg_cutoff         # run at several times of day
    uv run python -m spikes.alpaca_orders corp_scan          # upcoming splits / dividends
    uv run python -m spikes.alpaca_orders corp_setup KO PFE  # 1 share + GTC stop over an ex-date
    uv run python -m spikes.alpaca_orders corp_check         # after the ex-date
    uv run python -m spikes.alpaca_orders session --symbol F # market hours, ~20 min
    uv run python -m spikes.alpaca_orders opg_exit           # 19:00-09:28 ET, after `session`
    uv run python -m spikes.alpaca_orders opg_check          # after 09:31 ET
    uv run python -m spikes.alpaca_orders pdt --symbol SOFI  # market hours; uses up the day trades
    uv run python -m spikes.alpaca_orders cleanup [--flatten]
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest
from alpaca.trading.enums import (
    CorporateActionDateType,
    CorporateActionType,
    OrderClass,
    OrderSide,
    OrderStatus,
    QueryOrderStatus,
    TimeInForce,
)
from alpaca.trading.models import CorporateActionAnnouncement, Order, Position, TradeAccount
from alpaca.trading.requests import (
    GetCorporateAnnouncementsRequest,
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    OrderRequest,
    ReplaceOrderRequest,
    StopLimitOrderRequest,
    StopLossRequest,
    StopOrderRequest,
)

from spikes._common import Env, Log

HERE = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
CORP_STATE = HERE / "output" / "corp_state.json"
SESSION_STATE = HERE / "output" / "session_state.json"
DONE = {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED}
ORDER_FIELDS = (
    "id",
    "client_order_id",
    "symbol",
    "side",
    "type",
    "order_class",
    "time_in_force",
    "qty",
    "filled_qty",
    "filled_avg_price",
    "limit_price",
    "stop_price",
    "status",
    "extended_hours",
    "replaced_by",
    "replaces",
    "created_at",
    "filled_at",
    "expires_at",
)


class Spike:
    def __init__(self, name: str, account: str = "dev") -> None:
        self.env = Env(account)
        self.tc = self.env.trading()
        self.dc = self.env.data()
        self.log = Log(HERE, name, self.env.secrets())

    # --- helpers -------------------------------------------------------------------------

    def quote(self, symbol: str) -> tuple[float, float, float]:
        """(bid, ask, last) from IEX, the feed live trading sets its levels from."""
        q = self.dc.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
        )[symbol]
        t = self.dc.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
        )[symbol]
        self.log.event(
            "quote",
            symbol=symbol,
            bid=q.bid_price,
            ask=q.ask_price,
            quote_ts=q.timestamp,
            last=t.price,
            trade_ts=t.timestamp,
        )
        return float(q.bid_price), float(q.ask_price), float(t.price)

    def submit(self, label: str, req: OrderRequest) -> Order | None:
        req.client_order_id = req.client_order_id or coid(label)
        try:
            order = self.tc.submit_order(req)
        except APIError as exc:
            self.log.event(
                "submit_rejected", label=label, request=req, http=exc.status_code, body=str(exc)
            )
            return None
        assert isinstance(order, Order)
        self.log.event("submitted", label=label, order=brief(order))
        return order

    def replace(self, label: str, order_id: Any, req: ReplaceOrderRequest) -> Order | None:
        try:
            order = self.tc.replace_order_by_id(order_id, req)
        except APIError as exc:
            self.log.event(
                "replace_rejected", label=label, request=req, http=exc.status_code, body=str(exc)
            )
            return None
        assert isinstance(order, Order)
        self.log.event("replaced", label=label, order=brief(order))
        return order

    def cancel(self, label: str, order_id: Any) -> bool:
        try:
            self.tc.cancel_order_by_id(order_id)
        except APIError as exc:
            self.log.event("cancel_rejected", label=label, http=exc.status_code, body=str(exc))
            return False
        self.log.event("canceled", label=label, order_id=order_id)
        return True

    def get(self, order_id: Any) -> Order:
        order = self.tc.get_order_by_id(order_id)
        assert isinstance(order, Order)
        return order

    def show(self, label: str, order_id: Any) -> Order:
        order = self.get(order_id)
        self.log.event("order", label=label, order=brief(order))
        return order

    def wait(
        self, label: str, order_id: Any, until: Callable[[Order], bool], timeout_s: float
    ) -> Order:
        deadline = time.monotonic() + timeout_s
        order = self.get(order_id)
        seen = {order.status}
        while not until(order) and time.monotonic() < deadline:
            time.sleep(1.0)
            order = self.get(order_id)
            if order.status not in seen:
                seen.add(order.status)
                self.log.event("status_change", label=label, order=brief(order))
        self.log.event("wait_done", label=label, met=until(order), order=brief(order))
        return order

    def wait_settled(self, label: str, order_id: Any, timeout_s: float = 15) -> Order:
        """Wait for a just-submitted order to leave pending_new / accepted."""
        return self.wait(
            label,
            order_id,
            lambda o: o.status not in (OrderStatus.PENDING_NEW, OrderStatus.ACCEPTED),
            timeout_s,
        )

    def account(self) -> TradeAccount:
        acct = self.tc.get_account()
        assert isinstance(acct, TradeAccount)
        return acct

    def position_qty(self, symbol: str) -> int:
        try:
            pos = self.tc.get_open_position(symbol)
        except APIError:
            return 0
        assert isinstance(pos, Position)
        return int(float(pos.qty))

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        req = GetOrdersRequest(
            status=QueryOrderStatus.OPEN, nested=True, symbols=[symbol] if symbol else None
        )
        orders = self.tc.get_orders(req)
        return [o for o in orders if isinstance(o, Order)]

    def clock(self) -> None:
        clock = self.tc.get_clock()
        self.log.event("clock", clock=clock, now_et=datetime.now(ET))


def coid(label: str) -> str:
    return f"spk-{label}-{uuid4().hex[:8]}"[:48]


def tick(price: float) -> float:
    return round(price + 1e-9, 2)


def brief(order: Order) -> dict[str, Any]:
    out = {f: getattr(order, f) for f in ORDER_FIELDS}
    out["legs"] = [brief(leg) for leg in order.legs or []]
    return out


def filled(o: Order) -> bool:
    return o.status == OrderStatus.FILLED


def terminal(o: Order) -> bool:
    return o.status in DONE


def shares_for(price: float, notional: float) -> int:
    return max(1, math.floor(notional / price))


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


# --- any-time scenarios ----------------------------------------------------------------------


def scenario_account(args: argparse.Namespace) -> None:
    s = Spike("account")
    s.clock()
    acct = s.account()
    dump = acct.model_dump()
    dump["account_number"] = "..." + acct.account_number[-4:]
    dump["id"] = "..." + str(acct.id)[-4:]
    s.log.event("account_fields", fields=sorted(dump), account=dump)
    s.log.event("account_configuration", config=s.tc.get_account_configurations())
    s.log.event("positions", positions=s.tc.get_all_positions())
    s.log.event("open_orders", orders=[brief(o) for o in s.open_orders()])


def scenario_independence(args: argparse.Namespace) -> None:
    dev, paper = Spike("independence", "dev"), Spike("independence", "paper")
    a, b = dev.account(), paper.account()
    dev.log.event(
        "accounts",
        dev="..." + a.account_number[-4:],
        paper="..." + b.account_number[-4:],
        same_account=a.account_number == b.account_number,
        same_key=dev.env.key == paper.env.key,
    )
    _, _, last = dev.quote(args.symbol)
    order = dev.submit(
        "indep",
        LimitOrderRequest(
            symbol=args.symbol,
            qty=1,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=tick(last * 0.5),
        ),
    )
    if order is None:
        return
    seen_on_paper = [o for o in paper.open_orders() if o.client_order_id == order.client_order_id]
    dev.log.event("visible_on_other_account", visible=bool(seen_on_paper))
    dev.cancel("indep", order.id)


def scenario_dedupe(args: argparse.Namespace) -> None:
    s = Spike("dedupe")
    s.clock()
    _, _, last = s.quote(args.symbol)
    cid = coid("dedupe")

    def req() -> LimitOrderRequest:
        return LimitOrderRequest(
            symbol=args.symbol,
            qty=1,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=tick(last * 0.5),
            client_order_id=cid,
        )

    first = s.submit("dedupe_first", req())
    s.submit("dedupe_duplicate_while_open", req())
    if first is not None:
        s.cancel("dedupe_first", first.id)
        s.wait("dedupe_first", first.id, terminal, 15)
    s.submit("dedupe_duplicate_after_cancel", req())
    try:
        found = s.tc.get_order_by_client_id(cid)
        s.log.event(
            "lookup_by_client_id", order=brief(found) if isinstance(found, Order) else found
        )
    except APIError as exc:
        s.log.event("lookup_by_client_id_failed", http=exc.status_code, body=str(exc))


def scenario_oto_structure(args: argparse.Namespace) -> None:
    """OTO parents that can't fill (trigger far above / limit far below): inspect the legs."""
    s = Spike("oto_structure")
    s.clock()
    sym = args.symbol
    _, _, last = s.quote(sym)

    stop_limit = s.submit(
        "oto_stop_limit_day",
        StopLimitOrderRequest(
            symbol=sym,
            qty=1,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.OTO,
            stop_price=tick(last * 1.10),
            limit_price=tick(last * 1.11),
            stop_loss=StopLossRequest(stop_price=tick(last * 0.90)),
        ),
    )
    stop_limit_gtc = s.submit(
        "oto_stop_limit_gtc_parent",
        StopLimitOrderRequest(
            symbol=sym,
            qty=1,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
            order_class=OrderClass.OTO,
            stop_price=tick(last * 1.10),
            limit_price=tick(last * 1.11),
            stop_loss=StopLossRequest(stop_price=tick(last * 0.90)),
        ),
    )
    limit = s.submit(
        "oto_limit_day",
        LimitOrderRequest(
            symbol=sym,
            qty=1,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.OTO,
            limit_price=tick(last * 0.80),
            stop_loss=StopLossRequest(stop_price=tick(last * 0.70)),
        ),
    )
    stop_limit_leg = s.submit(
        "oto_stop_limit_with_stop_limit_leg",
        StopLimitOrderRequest(
            symbol=sym,
            qty=1,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.OTO,
            stop_price=tick(last * 1.10),
            limit_price=tick(last * 1.11),
            stop_loss=StopLossRequest(stop_price=tick(last * 0.90), limit_price=tick(last * 0.89)),
        ),
    )
    placed = [o for o in (stop_limit, stop_limit_gtc, limit, stop_limit_leg) if o is not None]
    for order in placed:
        s.show("after_submit", order.id)

    if stop_limit is not None and stop_limit.legs:
        leg = stop_limit.legs[0]
        s.replace(
            "leg_to_gtc_while_held", leg.id, ReplaceOrderRequest(time_in_force=TimeInForce.GTC)
        )
        s.replace(
            "leg_raise_stop_while_held",
            leg.id,
            ReplaceOrderRequest(stop_price=tick(last * 0.92)),
        )
        s.cancel("leg_cancel_while_held", leg.id)
        s.show("parent_after_leg_cancel_attempt", stop_limit.id)
        s.replace("parent_qty_change", stop_limit.id, ReplaceOrderRequest(qty=2))

    for order in placed:
        latest = s.get(order.id)
        target = latest.replaced_by or latest.id
        s.cancel("cleanup_parent", target)
        done = s.wait("cleanup_parent", target, terminal, 15)
        for leg in done.legs or []:
            s.show("leg_after_parent_cancel", leg.id)


def scenario_leg_cancel(args: argparse.Namespace) -> None:
    """A held OTO: change the parent's quantity, then cancel the untouched leg directly."""
    s = Spike("leg_cancel")
    _, _, last = s.quote(args.symbol)
    parent = s.submit(
        "oto_for_leg_cancel",
        StopLimitOrderRequest(
            symbol=args.symbol,
            qty=1,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.OTO,
            stop_price=tick(last * 1.10),
            limit_price=tick(last * 1.11),
            stop_loss=StopLossRequest(stop_price=tick(last * 0.90)),
        ),
    )
    if parent is None or not parent.legs:
        return
    replaced = s.replace("parent_qty_to_2", parent.id, ReplaceOrderRequest(qty=2))
    if replaced is not None:
        parent = s.wait_settled("parent_qty_to_2", replaced.id)
        s.show("parent_after_qty_change", parent.id)
    if not parent.legs:
        return
    leg = parent.legs[0]
    s.cancel("cancel_held_leg", leg.id)
    time.sleep(2)
    s.show("parent_after_leg_cancel", parent.id)
    s.show("leg_after_cancel", leg.id)
    if not terminal(s.get(parent.id)):
        s.cancel("cleanup_parent", parent.id)


def scenario_opg_cutoff(args: argparse.Namespace) -> None:
    s = Spike("opg_cutoff")
    s.clock()
    _, _, last = s.quote(args.symbol)
    loo = s.submit(
        "opg_limit_buy",
        LimitOrderRequest(
            symbol=args.symbol,
            qty=1,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.OPG,
            limit_price=tick(last * 0.5),
        ),
    )
    moo = s.submit(
        "opg_market_buy",
        MarketOrderRequest(
            symbol=args.symbol, qty=1, side=OrderSide.BUY, time_in_force=TimeInForce.OPG
        ),
    )
    for order in (loo, moo):
        if order is not None:
            s.show("opg_after_submit", order.id)
            s.cancel("opg_cleanup", order.id)


# --- corporate actions -------------------------------------------------------------------


def scenario_corp_scan(args: argparse.Namespace) -> None:
    s = Spike("corp_scan")
    today = datetime.now(ET).date()
    for ca_type in (CorporateActionType.SPLIT, CorporateActionType.DIVIDEND):
        anns = s.tc.get_corporate_announcements(
            GetCorporateAnnouncementsRequest(
                ca_types=[ca_type],
                since=today,
                until=today + timedelta(days=args.days),
                date_type=CorporateActionDateType.EX_DATE,
            )
        )
        rows = [a for a in anns if isinstance(a, CorporateActionAnnouncement)]
        s.log.event(
            "announcements",
            ca_type=ca_type,
            count=len(rows),
            rows=[
                {
                    "symbol": a.target_symbol or a.initiating_symbol,
                    "sub_type": a.ca_sub_type,
                    "ex_date": a.ex_date,
                    "cash": a.cash,
                    "old_rate": a.old_rate,
                    "new_rate": a.new_rate,
                }
                for a in rows[:200]
            ],
        )


def scenario_corp_setup(args: argparse.Namespace) -> None:
    """Buy 1 share of each symbol and rest a GTC sell stop 15% below, to watch over an ex-date."""
    s = Spike("corp_setup")
    s.clock()
    after_hours = not s.tc.get_clock().is_open  # type: ignore[union-attr]
    state = load_state(CORP_STATE)
    for sym in args.symbols:
        _, ask, last = s.quote(sym)
        buy = s.submit(
            f"corp_buy_{sym}",
            LimitOrderRequest(
                symbol=sym,
                qty=1,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=tick(max(ask, last) * 1.01),
                extended_hours=after_hours,
            ),
        )
        if buy is None:
            continue
        buy = s.wait(f"corp_buy_{sym}", buy.id, terminal, 60)
        if not filled(buy):
            s.cancel(f"corp_buy_{sym}", buy.id)
            continue
        fill = float(buy.filled_avg_price or last)
        stop = s.submit(
            f"corp_stop_{sym}",
            StopOrderRequest(
                symbol=sym,
                qty=1,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                stop_price=tick(fill * 0.85),
            ),
        )
        if stop is not None:
            state[sym] = {
                "stop_id": str(stop.id),
                "stop_price": float(stop.stop_price or 0),
                "qty": 1,
                "placed_at": datetime.now(UTC).isoformat(),
            }
    save_state(CORP_STATE, state)


def scenario_corp_check(args: argparse.Namespace) -> None:
    s = Spike("corp_check")
    state = load_state(CORP_STATE)
    for sym, rec in state.items():
        order = s.get(rec["stop_id"])
        chain = [brief(order)]
        while order.replaced_by:
            order = s.get(order.replaced_by)
            chain.append(brief(order))
        s.log.event(
            "corp_stop",
            symbol=sym,
            placed_stop=rec["stop_price"],
            placed_qty=rec["qty"],
            now_stop=order.stop_price,
            now_qty=order.qty,
            status=order.status,
            position_qty=s.position_qty(sym),
            chain=chain,
        )


# --- market-hours scenarios --------------------------------------------------------------


def oto_entry(
    s: Spike,
    label: str,
    sym: str,
    qty: int,
    stop_price: float,
    tif: TimeInForce = TimeInForce.DAY,
) -> Order | None:
    """An OTO buy-stop-limit just above the market, re-placed with a fresh trigger if unfilled.

    The leg inherits the parent's time-in-force (oto_structure), so a GTC parent gives a GTC stop.
    """
    for attempt in range(6):
        _, ask, last = s.quote(sym)
        trigger = tick(max(ask, last) + 0.01)
        order = s.submit(
            f"{label}_try{attempt}",
            StopLimitOrderRequest(
                symbol=sym,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=tif,
                order_class=OrderClass.OTO,
                stop_price=trigger,
                limit_price=tick(trigger * 1.005),
                stop_loss=StopLossRequest(stop_price=stop_price),
            ),
        )
        if order is None:
            return None
        order = s.wait(label, order.id, terminal, 120)
        if order.status == OrderStatus.FILLED:
            return order
        if order.status == OrderStatus.PARTIALLY_FILLED or float(order.filled_qty or 0) > 0:
            s.log.event("partial_fill_seen", label=label, order=brief(order))
            return order
        s.cancel(label, order.id)
        s.wait(label, order.id, terminal, 15)
    return None


def marketable_sell(s: Spike, label: str, sym: str, qty: int) -> Order | None:
    bid, _, last = s.quote(sym)
    order = s.submit(
        label,
        LimitOrderRequest(
            symbol=sym,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            limit_price=tick(min(bid, last) * 0.995),
        ),
    )
    if order is None:
        return None
    return s.wait(label, order.id, terminal, 30)


def scenario_session(args: argparse.Namespace) -> None:
    s = Spike("session")
    s.clock()
    sym = args.symbol
    _, ask, last = s.quote(sym)
    q = shares_for(last, args.notional)
    plan_stop = tick(last * 0.93)
    s.log.event("plan", symbol=sym, tranche_qty=q, plan_stop=plan_stop)

    # 1. A buy stop with the trigger at or below the market: rejected, or triggered at once?
    below = s.submit(
        "buy_stop_trigger_below_market",
        StopLimitOrderRequest(
            symbol=sym,
            qty=1,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.OTO,
            stop_price=tick(last * 0.99),
            limit_price=tick(last * 0.995),
            stop_loss=StopLossRequest(stop_price=plan_stop),
        ),
    )
    if below is not None:
        below = s.wait("buy_stop_trigger_below_market", below.id, terminal, 20)
        if not terminal(below):
            s.cancel("buy_stop_trigger_below_market", below.id)
        elif filled(below):
            # Its leg would reserve the share; the merged full stop below covers it instead.
            for leg in below.legs or []:
                s.cancel("below_market_leg", leg.id)
                s.wait("below_market_leg", leg.id, terminal, 15)

    # 2. Tranche 1: OTO buy-stop-limit with a stop leg; inspect the leg once filled.
    t1 = oto_entry(s, "tranche1", sym, q, plan_stop)
    if t1 is None:
        s.log.event("abort", reason="tranche 1 never filled")
        return
    leg1 = t1.legs[0] if t1.legs else None
    if leg1 is None:
        s.log.event("abort", reason="filled OTO has no legs")
        return
    leg1 = s.show("tranche1_leg_after_fill", leg1.id)
    s.replace("leg1_tif_to_gtc", leg1.id, ReplaceOrderRequest(time_in_force=TimeInForce.GTC))
    leg1 = s.show("tranche1_leg_after_tif_replace", s.get(leg1.id).replaced_by or leg1.id)

    # 3. Tranche 2 with a GTC parent (so its leg is GTC), then merge into one full-quantity stop:
    #    keep the GTC leg, cancel the day leg, grow the GTC leg to the whole position.
    merged: Order | None = None
    t2 = oto_entry(s, "tranche2", sym, q, plan_stop, tif=TimeInForce.GTC)
    if t2 is not None and t2.legs:
        leg2 = s.show("tranche2_leg_after_fill", t2.legs[0].id)
        total = s.position_qty(sym)
        s.replace("leg2_qty_to_full_while_leg1_rests", leg2.id, ReplaceOrderRequest(qty=total))
        s.cancel("cancel_day_leg1", leg1.id)
        s.wait("cancel_day_leg1", leg1.id, terminal, 15)
        s.show("tranche1_parent_after_leg_cancel", t1.id)
        merged = s.replace(
            "leg2_qty_to_full_after_leg1_cancel",
            s.get(leg2.id).replaced_by or leg2.id,
            ReplaceOrderRequest(qty=total),
        )
        if merged is not None:
            merged = s.wait_settled("merged_stop", merged.id)
    total = s.position_qty(sym)
    if merged is None or merged.status not in (OrderStatus.NEW, OrderStatus.HELD):
        # Fallback: cancel whatever leg remains and place one standalone GTC stop.
        for o in s.open_orders(sym):
            if o.side == OrderSide.SELL:
                s.cancel("cancel_remaining_leg", o.id)
                s.wait("cancel_remaining_leg", o.id, terminal, 15)
        merged = s.submit(
            "standalone_full_gtc_stop",
            StopOrderRequest(
                symbol=sym,
                qty=total,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                stop_price=plan_stop,
            ),
        )
        if merged is None:
            s.log.event("abort", reason="could not place the full stop: position unprotected")
            return
    stop = s.wait_settled("full_stop", merged.id)
    s.log.event("full_stop_in_place", position_qty=total, stop=brief(stop))

    # 4. Share reservation: a sell while the full-quantity stop rests.
    rejected = marketable_sell(s, "sell_1_while_full_stop_rests", sym, 1)
    if rejected is not None and filled(rejected):
        s.log.event("reservation", note="sell filled despite full stop: shares NOT reserved")

    # 5. Sell sequence, success: reduce stop -> sell 1 -> stop covers the rest.
    total = s.position_qty(sym)
    reduced = s.replace("reduce_stop_for_partial", stop.id, ReplaceOrderRequest(qty=total - 1))
    if reduced is not None:
        stop = s.wait_settled("reduced_stop", reduced.id)
        marketable_sell(s, "partial_sell_1", sym, 1)
        s.log.event("after_partial", position_qty=s.position_qty(sym), stop=brief(s.get(stop.id)))

    # 6. Sell sequence, failure: reduce -> unfillable sell -> cancel -> restore.
    total = s.position_qty(sym)
    if total >= 2:
        reduced = s.replace(
            "reduce_stop_for_failed_sell", stop.id, ReplaceOrderRequest(qty=total - 1)
        )
        if reduced is not None:
            stop = s.wait_settled("reduced_stop", reduced.id)
            _, _, last = s.quote(sym)
            unfillable = s.submit(
                "sell_that_wont_fill",
                LimitOrderRequest(
                    symbol=sym,
                    qty=1,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                    limit_price=tick(last * 1.05),
                ),
            )
            if unfillable is not None:
                s.wait("sell_that_wont_fill", unfillable.id, terminal, 30)
                s.cancel("sell_that_wont_fill", unfillable.id)
                s.wait("sell_that_wont_fill", unfillable.id, terminal, 15)
            restored = s.replace("restore_stop", stop.id, ReplaceOrderRequest(qty=total))
            if restored is not None:
                stop = s.wait_settled("restored_stop", restored.id)

    # 7. A sell stop at or above the market: rejected, or triggered at once?
    total = s.position_qty(sym)
    if total >= 2:
        reduced = s.replace(
            "reduce_stop_for_above_test", stop.id, ReplaceOrderRequest(qty=total - 1)
        )
        if reduced is not None:
            stop = s.wait_settled("reduced_stop", reduced.id)
            _, ask, last = s.quote(sym)
            above = s.submit(
                "sell_stop_above_market",
                StopOrderRequest(
                    symbol=sym,
                    qty=1,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.GTC,
                    stop_price=tick(max(ask, last) * 1.01),
                ),
            )
            if above is not None:
                above = s.wait("sell_stop_above_market", above.id, terminal, 20)
                if not terminal(above):
                    s.cancel("sell_stop_above_market", above.id)
                    s.wait("sell_stop_above_market", above.id, terminal, 15)
            restored = s.replace(
                "restore_after_above_test", stop.id, ReplaceOrderRequest(qty=s.position_qty(sym))
            )
            if restored is not None:
                stop = s.wait_settled("restored_stop", restored.id)

    # 8. Marketable-limit add, then grow the stop to the new quantity.
    _, ask, last = s.quote(sym)
    add = s.submit(
        "marketable_limit_add",
        LimitOrderRequest(
            symbol=sym,
            qty=1,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=tick(max(ask, last) * 1.003),
        ),
    )
    if add is not None:
        s.wait("marketable_limit_add", add.id, terminal, 30)
        grown = s.replace(
            "grow_stop_after_add", stop.id, ReplaceOrderRequest(qty=s.position_qty(sym))
        )
        if grown is not None:
            stop = s.wait_settled("grown_stop", grown.id)

    # 9. Raise the stop (tighten-only path) and leave it resting overnight for opg_exit.
    _, _, last = s.quote(sym)
    raised = s.replace("raise_stop", stop.id, ReplaceOrderRequest(stop_price=tick(last * 0.95)))
    if raised is not None:
        stop = s.wait_settled("raised_stop", raised.id)
    acct = s.account()
    s.log.event(
        "end_of_session",
        position_qty=s.position_qty(sym),
        stop=brief(s.get(stop.id)),
        daytrade_count=acct.daytrade_count,
        pattern_day_trader=acct.pattern_day_trader,
        open_orders=[brief(o) for o in s.open_orders(sym)],
    )
    save_state(SESSION_STATE, {"symbol": sym, "stop_id": str(stop.id)})


def current_stop(s: Spike, state: dict[str, Any]) -> Order:
    order = s.get(state["stop_id"])
    while order.replaced_by:
        order = s.get(order.replaced_by)
    return order


def scenario_opg_exit(args: argparse.Namespace) -> None:
    """Before the open: did the GTC stop survive the night? Then an OPG trim (sell sequence)."""
    s = Spike("opg_exit")
    s.clock()
    state = load_state(SESSION_STATE)
    sym = state["symbol"]
    stop = current_stop(s, state)
    s.log.event("overnight_stop", stop=brief(stop))
    total = s.position_qty(sym)
    reduced = s.replace("reduce_stop_for_opg", stop.id, ReplaceOrderRequest(qty=total - 1))
    if reduced is None:
        return
    state["stop_id"] = str(reduced.id)
    opg = s.submit(
        "opg_market_sell_1",
        MarketOrderRequest(symbol=sym, qty=1, side=OrderSide.SELL, time_in_force=TimeInForce.OPG),
    )
    if opg is not None:
        state["opg_id"] = str(opg.id)
    else:
        s.replace("restore_after_opg_reject", reduced.id, ReplaceOrderRequest(qty=total))
    save_state(SESSION_STATE, state)


def scenario_opg_check(args: argparse.Namespace) -> None:
    s = Spike("opg_check")
    s.clock()
    state = load_state(SESSION_STATE)
    sym = state["symbol"]
    if "opg_id" in state:
        s.show("opg_after_open", state["opg_id"])
    stop = current_stop(s, state)
    total = s.position_qty(sym)
    s.log.event("after_open", position_qty=total, stop=brief(stop))
    if float(stop.qty or 0) != total:
        restored = s.replace("restore_stop_after_opg", stop.id, ReplaceOrderRequest(qty=total))
        if restored is not None:
            state["stop_id"] = str(restored.id)
            save_state(SESSION_STATE, state)


def scenario_pdt(args: argparse.Namespace) -> None:
    """Use up the day-trade allowance, then try an entry with a stop leg and a same-day exit."""
    s = Spike("pdt")
    s.clock()
    sym = args.symbol
    acct = s.account()
    s.log.event("pdt_start", daytrade_count=acct.daytrade_count, pdt=acct.pattern_day_trader)
    for i in range(4):
        if int(acct.daytrade_count or 0) >= 3:
            break
        _, ask, last = s.quote(sym)
        buy = s.submit(
            f"roundtrip{i}_buy",
            LimitOrderRequest(
                symbol=sym,
                qty=1,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=tick(max(ask, last) * 1.003),
            ),
        )
        if buy is None or not filled(s.wait(f"roundtrip{i}_buy", buy.id, terminal, 30)):
            break
        marketable_sell(s, f"roundtrip{i}_sell", sym, 1)
        time.sleep(2)
        acct = s.account()
        s.log.event("daytrade_count", count=acct.daytrade_count)

    # At the allowance limit: an OTO entry with a stop leg (the leg could close same day).
    _, ask, last = s.quote(sym)
    entry = s.submit(
        "oto_entry_at_pdt_limit",
        LimitOrderRequest(
            symbol=sym,
            qty=1,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.OTO,
            limit_price=tick(max(ask, last) * 1.003),
            stop_loss=StopLossRequest(stop_price=tick(last * 0.95)),
        ),
    )
    if entry is not None:
        entry = s.wait("oto_entry_at_pdt_limit", entry.id, terminal, 30)
        for leg in entry.legs or []:
            s.show("pdt_leg", leg.id)
    # A plain buy, then a standalone stop on today's shares, then a same-day exit.
    plain = s.submit(
        "plain_buy_at_pdt_limit",
        LimitOrderRequest(
            symbol=sym,
            qty=1,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=tick(max(ask, last) * 1.003),
        ),
    )
    if plain is not None and filled(s.wait("plain_buy_at_pdt_limit", plain.id, terminal, 30)):
        stop = s.submit(
            "standalone_stop_same_day_shares",
            StopOrderRequest(
                symbol=sym,
                qty=1,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                stop_price=tick(last * 0.95),
            ),
        )
        if stop is not None:
            s.cancel("standalone_stop_same_day_shares", stop.id)
            s.wait("standalone_stop_same_day_shares", stop.id, terminal, 15)
        marketable_sell(s, "same_day_exit_at_pdt_limit", sym, 1)
        sell_stop = s.submit(
            "stop_through_market_at_pdt_limit",
            StopOrderRequest(
                symbol=sym,
                qty=1,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                stop_price=tick(last * 1.01),
            ),
        )
        if sell_stop is not None:
            s.wait("stop_through_market_at_pdt_limit", sell_stop.id, terminal, 20)
    acct = s.account()
    s.log.event(
        "pdt_end",
        daytrade_count=acct.daytrade_count,
        pdt=acct.pattern_day_trader,
        position_qty=s.position_qty(sym),
        open_orders=[brief(o) for o in s.open_orders(sym)],
    )


def scenario_cleanup(args: argparse.Namespace) -> None:
    s = Spike("cleanup")
    keep = {rec["stop_id"] for rec in load_state(CORP_STATE).values()}
    keep_symbols = set(load_state(CORP_STATE))
    for order in s.open_orders():
        if order.client_order_id.startswith("spk-") and str(order.id) not in keep:
            if order.symbol in keep_symbols and order.side == OrderSide.SELL:
                continue
            s.cancel("cleanup", order.id)
    if args.flatten:
        for pos in s.tc.get_all_positions():
            assert isinstance(pos, Position)
            if pos.symbol not in keep_symbols:
                s.log.event("flatten", symbol=pos.symbol, qty=pos.qty)
                s.tc.close_position(pos.symbol)


def main() -> None:
    parser = argparse.ArgumentParser(prog="spikes.alpaca_orders")
    sub = parser.add_subparsers(dest="scenario", required=True)
    for name in ("account", "corp_check", "opg_exit", "opg_check"):
        sub.add_parser(name)
    for name in ("independence", "dedupe", "oto_structure", "leg_cancel", "opg_cutoff"):
        sub.add_parser(name).add_argument("--symbol", default="F")
    sub.add_parser("corp_scan").add_argument("--days", type=int, default=14)
    sub.add_parser("corp_setup").add_argument("symbols", nargs="+")
    p = sub.add_parser("session")
    p.add_argument("--symbol", default="F")
    p.add_argument("--notional", type=float, default=80.0, help="dollars per tranche")
    sub.add_parser("pdt").add_argument("--symbol", default="SOFI")
    sub.add_parser("cleanup").add_argument("--flatten", action="store_true")
    args = parser.parse_args()
    globals()[f"scenario_{args.scenario}"](args)


if __name__ == "__main__":
    main()

"""What every order job shares (README §5.7): its dependencies, the order section (lock →
reconcile → desk), the stop invariant, owed sales, and the checks that block entries.

An order job runs:

    with order_section(ctx, deps) as section:
        report = enforce_invariant(section)
        ...its own work, through section.desk...

`dry_run` (the default trading mode) reconciles and reports what the invariant would repair,
and changes no order: only `TRADING_MODE=paper` sends orders (README §14).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog
from sqlalchemy import Engine

from auratrade.calendar.sessions import MarketCalendar, load_calendar
from auratrade.config import TradingMode
from auratrade.db.flags import read_flags
from auratrade.execution import store
from auratrade.execution.broker import Broker, PriceSource
from auratrade.execution.invariant import check
from auratrade.execution.lock import Holder
from auratrade.execution.orders import OrderDesk, SaleStatus
from auratrade.execution.reconcile import Reconciled, reconcile
from auratrade.jobs.base import JobContext
from auratrade.notify.alerts import Alerter

log = structlog.get_logger(__name__)


class ExecutionError(RuntimeError):
    pass


class DryRunViolationError(RuntimeError):
    """An order call in dry_run: a bug, stopped before it reached the broker."""


class ReadOnlyBroker:
    """The broker for a dry run: reads pass through, every order change raises."""

    def __init__(self, inner: Broker) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        if name in ("submit", "replace", "cancel"):
            raise DryRunViolationError(f"{name} called in dry_run")
        return getattr(self._inner, name)


class Lock(Protocol):
    def hold(self, job: str, *, wait: bool = True) -> AbstractContextManager[bool]: ...

    def holder(self) -> Holder | None: ...


@dataclass(frozen=True)
class ExecutionDeps:
    engine: Engine
    broker: Broker
    prices: PriceSource
    lock: Lock
    alerter: Alerter
    sleep: Callable[[float], None]
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


DepsFactory = Callable[[JobContext], ExecutionDeps]


def production_deps(ctx: JobContext) -> ExecutionDeps:
    import time

    from auratrade.data.alpaca_data import AlpacaDataClient, alpaca_http
    from auratrade.db.engine import engine_from_settings
    from auratrade.execution.alpaca_broker import AlpacaBroker, AlpacaPrices
    from auratrade.execution.lock import OrderLock
    from auratrade.jobs.wiring import make_alerter

    settings = ctx.settings
    if settings.alpaca_api_key is None or settings.alpaca_secret_key is None:
        raise ExecutionError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
    if settings.database_url is None:
        raise ExecutionError("DATABASE_URL is not set")
    key = settings.alpaca_api_key.get_secret_value()
    secret = settings.alpaca_secret_key.get_secret_value()
    alerter = make_alerter(settings)
    cfg = ctx.risk.execution
    return ExecutionDeps(
        engine=engine_from_settings(settings),
        broker=AlpacaBroker.paper(key, secret),
        prices=AlpacaPrices(AlpacaDataClient(alpaca_http(key, secret)), cfg.decision_price),
        lock=OrderLock(
            settings.database_url.get_secret_value(),
            cfg,
            alert=_alert_fn(ctx, alerter, []),
        ),
        alerter=alerter,
        sleep=time.sleep,
    )


def _alert_fn(ctx: JobContext, alerter: Alerter, sink: list[str]) -> Callable[[str], None]:
    prefix = f"[AuraTrade {ctx.settings.environment}] {ctx.job} {ctx.session_date}: "

    def alert(text: str) -> None:
        sink.append(text)
        ctx.log.warning("order_alert", text=text)
        try:
            alerter.alert(prefix + text)
        except Exception as exc:
            # An alert that fails must never stop an order sequence half-way.
            ctx.log.error("alert_failed", text=text, error=f"{type(exc).__name__}: {exc}")

    return alert


@dataclass
class Section:
    ctx: JobContext
    deps: ExecutionDeps
    calendar: MarketCalendar
    rec: Reconciled
    desk: OrderDesk
    alerts: list[str]

    @property
    def dry_run(self) -> bool:
        return self.ctx.trading_mode is not TradingMode.PAPER

    def alert(self, text: str) -> None:
        self.desk.alert(text)


@contextmanager
def order_section(ctx: JobContext, deps: ExecutionDeps) -> Iterator[Section]:
    """Wait for the order lock, reconcile, and yield a desk (README §5.7.7)."""
    with deps.lock.hold(ctx.job, wait=True):
        yield _section(ctx, deps)


@contextmanager
def try_order_section(ctx: JobContext, deps: ExecutionDeps) -> Iterator[Section | None]:
    """The same without waiting: None while another job holds the lock (`stop_guard`)."""
    with deps.lock.hold(ctx.job, wait=False) as held:
        yield _section(ctx, deps) if held else None


def _section(ctx: JobContext, deps: ExecutionDeps) -> Section:
    alerts: list[str] = []
    alert = _alert_fn(ctx, deps.alerter, alerts)
    with deps.engine.connect() as conn:
        calendar = load_calendar(conn)
    rec = reconcile(
        deps.engine,
        deps.broker,
        session=ctx.session_date,
        now=deps.clock(),
        calendar=calendar,
        risk=ctx.risk,
    )
    for text in rec.alerts:
        alert(text)
    dry_run = ctx.trading_mode is not TradingMode.PAPER
    broker: Broker = ReadOnlyBroker(deps.broker) if dry_run else deps.broker
    desk = OrderDesk(
        broker=broker,
        prices=deps.prices,
        cfg=ctx.risk.execution,
        session=ctx.session_date,
        market_open=deps.broker.is_market_open(),
        sleep=deps.sleep,
        alert=alert,
        log=ctx.log,
    )
    return Section(ctx, deps, calendar, rec, desk, alerts)


@dataclass
class InvariantReport:
    checked: int
    problems: dict[str, list[str]] = field(default_factory=dict)
    repaired: list[str] = field(default_factory=list)
    still_unprotected: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """Nothing was wrong at the start of the run (entries may go ahead)."""
        return not self.problems

    def details(self) -> dict[str, Any]:
        return {
            "positions_checked": self.checked,
            "problems": self.problems,
            "repaired": self.repaired,
            "still_unprotected": self.still_unprotected,
        }


def enforce_invariant(section: Section) -> InvariantReport:
    """Check every position's stops against its plan level; repair what is wrong (README
    §5.7.1). A repair alerts, and the caller blocks entries for the run."""
    broker, rec, desk = section.deps.broker, section.rec, section.desk
    protections = check(broker.positions(), broker.open_orders(), rec.levels)
    report = InvariantReport(checked=len(rec.positions))
    bad = {s: p for s, p in protections.items() if not p.ok}
    report.problems = {s: sorted(x.value for x in p.problems) for s, p in bad.items()}
    if not bad:
        return report
    if section.dry_run:
        report.still_unprotected = sorted(bad)
        section.ctx.log.warning("invariant_dry_run", problems=report.problems)
        return report
    for symbol, protection in bad.items():
        level = rec.levels.get(symbol)
        if level is None and protection.qty > 0:
            # A position the database doesn't know: no plan level to place a stop at.
            section.alert(f"{symbol}: {protection.qty} shares with no plan; not touched")
            report.still_unprotected.append(symbol)
            continue
        result = desk.protect(symbol, level or 0.0)
        report.repaired.append(symbol)
        if result.sale is not None and result.sale.owed:
            mark_exit_pending(section, symbol, "HARD_STOP")
    after = check(broker.positions(), broker.open_orders(), rec.levels)
    report.still_unprotected += [
        s for s, p in after.items() if not p.ok and s not in report.still_unprotected
    ]
    section.alert(
        f"stop invariant repaired for {', '.join(report.repaired) or 'nothing'} "
        f"({report.problems}); entries blocked this run"
        + (f"; STILL UNPROTECTED: {report.still_unprotected}" if report.still_unprotected else "")
    )
    return report


def mark_exit_pending(section: Section, symbol: str, reason: str | None) -> None:
    position = section.rec.positions.get(symbol)
    if position is None:
        return
    with section.deps.engine.begin() as conn:
        store.set_exit_pending(conn, position.id, reason, section.deps.clock())


def sell_owed(section: Section) -> list[dict[str, Any]]:
    """Retry the sales the plan decided and that haven't happened (README §5.7.2, #124)."""
    out: list[dict[str, Any]] = []
    if section.dry_run or not section.desk.market_open:
        return out
    for symbol, position in section.rec.positions.items():
        if position.exit_pending is None:
            continue
        level = section.rec.levels[symbol]
        result = section.desk.sell(symbol, position.shares, position.exit_pending, level)
        if result.status is SaleStatus.FILLED:
            mark_exit_pending(section, symbol, None)
        out.append({"symbol": symbol, "reason": position.exit_pending, **_sale(result)})
    return out


def _sale(result: Any) -> dict[str, Any]:
    return {"requested": result.requested, "sold": result.sold, "status": result.status.value}


def enforce_kill_switch(section: Section) -> bool:
    """True when entries are disabled; the kill switch also cancels resting entry orders."""
    with section.deps.engine.connect() as conn:
        flags = read_flags(conn)
    if not flags.trading_enabled and not section.dry_run:
        count = section.desk.cancel_entries(why="kill switch")
        if count:
            section.alert(f"kill switch on: cancelled {count} resting entry orders")
    return not flags.entries_allowed


def entry_blocks(section: Section, invariant: InvariantReport) -> list[str]:
    """Why no new entry may go in this run (fail closed, README §3 rule 6). Empty: entries
    may go ahead."""
    reasons: list[str] = []
    ctx = section.ctx
    with section.deps.engine.connect() as conn:
        flags = read_flags(conn)
        mismatches = store.open_mismatches(conn)
    if not flags.trading_enabled:
        reasons.append("kill switch on")
    if flags.halt_daily_loss or flags.halt_weekly_loss or flags.halt_drawdown:
        reasons.append("loss or drawdown halt")
    session = section.calendar.session_for(ctx.session_date)
    if session is None:
        reasons.append("not a session")
    else:
        if not session.calendar_agrees:
            reasons.append(f"calendar cross-check {session.calendar_check}")
        if session.is_early_close:
            reasons.append("early close: no new entries")
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

"""What every order job shares (README §5.6): its dependencies, the order section (lock →
reconcile → desk), the stop invariant on the portfolio's funds, and the kill switch.

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
from auratrade.execution import funds
from auratrade.execution.broker import Broker, PriceSource
from auratrade.execution.invariant import check
from auratrade.execution.lock import Holder
from auratrade.execution.orders import OrderDesk
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
    """Wait for the order lock, reconcile, and yield a desk (README §5.6.5)."""
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
    portfolio = ctx.risk.portfolio
    rec = reconcile(deps.engine, deps.broker, now=deps.clock(), funds=funds.funds(portfolio))
    with deps.engine.begin() as conn:
        rec.alerts += funds.apply_splits(
            conn, portfolio, rec.broker_positions, session=ctx.session_date, now=deps.clock()
        )
        rec.levels.update(funds.levels(conn, portfolio, rec.broker_positions))
        blocked = set(funds.mismatched(conn, portfolio))
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
        blocked_sales=blocked,
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
    """Check the funds' stops against the leveraged fund's level; repair what is wrong (README
    §5.6.1). A repair alerts, and the caller blocks increases for the run. Positions outside the
    funds are reconciliation's mismatches: not the portfolio's, left alone."""
    broker, rec, desk = section.deps.broker, section.rec, section.desk
    portfolio = section.ctx.risk.portfolio
    names, unstopped = funds.funds(portfolio), funds.unstopped(portfolio)

    def protections() -> dict[str, Any]:
        positions = [p for p in broker.positions() if p.symbol in names]
        orders = [o for o in broker.open_orders() if o.symbol in names]
        return check(positions, orders, rec.levels, unstopped=unstopped)

    report = InvariantReport(checked=len([s for s in rec.broker_positions if s in names]))
    bad = {s: p for s, p in protections().items() if not p.ok}
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
            # The leveraged fund held without a recorded level: bought outside the order layer.
            section.alert(f"{symbol}: {protection.qty} shares with no plan; not touched")
            report.still_unprotected.append(symbol)
            continue
        desk.protect(symbol, level or 0.0)
        report.repaired.append(symbol)
    report.still_unprotected += [
        s for s, p in protections().items() if not p.ok and s not in report.still_unprotected
    ]
    section.alert(
        f"stop invariant repaired for {', '.join(report.repaired) or 'nothing'} "
        f"({report.problems}); increases blocked this run"
        + (f"; STILL UNPROTECTED: {report.still_unprotected}" if report.still_unprotected else "")
    )
    return report


def enforce_kill_switch(section: Section) -> bool:
    """True when the kill switch is on; it also cancels resting buy orders (README §5.9)."""
    with section.deps.engine.connect() as conn:
        flags = read_flags(conn)
    if not flags.trading_enabled and not section.dry_run:
        count = section.desk.cancel_entries(why="kill switch")
        if count:
            section.alert(f"kill switch on: cancelled {count} resting buy orders")
    return not flags.trading_enabled

"""``evaluate(actions, context)``: every LLM action through the guardrails (archived README §8).

The Portfolio Manager proposes a regime view, entries, tightenings and exits; the EOD review
proposes tranche-2 adds, tightenings and exits. Each action is approved, clipped or rejected.
Actions are taken in order, and each approved entry counts against the heat, exposure, position
and cluster limits of the ones after it. The regime view is applied first: the effective regime
is never above the computed one (the regime ceiling).

Entries are checked in this order, and the first rejection ends the check:
blocks -> tradable symbol -> regime / fundability -> exit menu -> price levels -> stop width
(on the planned limit) -> 50-day extension -> base count -> earnings blackout -> max risk ->
heat -> clusters -> positions -> whole shares and position size -> exposure / no margin.

The checks at 09:36, on the real trigger, limit and stop, are in ``guardrails.orders``.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import assert_never

from auratrade.config import ExitPlan, Regime, RiskConfig, StopKind, Strategy
from auratrade.execution.sizing import planned_shares
from auratrade.execution.triggers import limit_price
from auratrade.guardrails.blocks import EntryBlock, entries_blocked
from auratrade.guardrails.clusters import ClusterMember, cluster_check
from auratrade.guardrails.earnings import earnings_blackout
from auratrade.guardrails.levels import StrategyRecord, extension, price_levels, stop_width
from auratrade.guardrails.model import (
    Finding,
    GuardrailRule,
    Outcome,
    clip,
    finite_positive,
    outcome_of,
    reject,
)
from auratrade.guardrails.positions import exit_check, tighten_check, tranche2_check
from auratrade.guardrails.risk import (
    OpenRisk,
    RegimeLimits,
    base_count_factor,
    effective_risk,
    exposure_check,
    fundable,
    heat_room,
    positions_check,
    regime_limits,
    risk_per_trade,
)
from auratrade.plans.entry import GateResult
from auratrade.plans.model import PositionPlan
from auratrade.plans.versioning import Tightening
from auratrade.regime.ladder import downgrade

# ---- context -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SetupFacts:
    """Code's view of one of today's fundable setups or approved EP candidates."""

    symbol: str
    strategy: Strategy
    pivot: float
    """The code pivot."""
    structural_stop: float
    """The lowest stop allowed; a momentum / EP setup's invalidation level."""
    price: float
    """The latest close (the 50-day check and the planned exposure)."""
    atr: float | None
    adr: float | None
    """A fraction; an EP's pre-gap ADR."""
    sma_50: float | None
    base_stage: int | None
    base_reliable: bool
    industry: str | None
    closes: Sequence[float]
    """For the cluster correlation, aligned with every holding's."""
    next_report_in: int | None
    last_report_ago: int | None


@dataclass(frozen=True, slots=True)
class Holding:
    """A held position."""

    symbol: str
    shares: int
    price: float
    plan: PositionPlan
    industry: str | None
    closes: Sequence[float]
    session: int
    """Sessions since entry (the entry session is 1)."""
    conviction: int | None
    """The entry's conviction (tranche 2 must still clear the regime's bar)."""
    tranche2_blocked: bool = False
    """A close below the pivot on light volume blocked tranche 2 (``plans.resolve``)."""
    next_report_in: int | None = None
    last_report_ago: int | None = None

    @property
    def open_risk(self) -> OpenRisk:
        return OpenRisk(self.shares, self.plan.entry_price, self.plan.hard_stop, self.price)


@dataclass(frozen=True, slots=True)
class PendingEntry:
    """An entry approved earlier and not yet filled or cancelled: its full planned risk and
    reserved exposure count."""

    symbol: str
    risk_amount: float
    reserved_exposure: float
    industry: str | None
    closes: Sequence[float]


@dataclass(frozen=True, slots=True)
class Context:
    equity: float
    regime: Regime
    """The computed regime."""
    early_uptrend_stepped: bool
    """``regime.state.early_uptrend_stepped`` for this session."""
    multiplier: float
    """The progressive-exposure multiplier (``journal.risk_multiplier``)."""
    blocks: frozenset[EntryBlock]
    setups: Mapping[tuple[str, Strategy], SetupFacts]
    holdings: Mapping[str, Holding]
    pending: tuple[PendingEntry, ...] = ()
    records: Mapping[Strategy, StrategyRecord] = field(default_factory=dict)


# ---- actions -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegimeView:
    regime: Regime | None


@dataclass(frozen=True, slots=True)
class Entry:
    symbol: str
    strategy: Strategy
    pivot: float
    stop: float | None
    """A pivot strategy's initial stop; a momentum / EP setup's invalidation level."""
    risk: float | None
    """A fraction of equity; None takes the effective risk."""
    conviction: int | None
    exits: ExitPlan | None = None
    """The exit-menu choice; None takes the strategy's default plan."""


@dataclass(frozen=True, slots=True)
class Tranche2:
    symbol: str
    gate: GateResult


@dataclass(frozen=True, slots=True)
class Tighten:
    symbol: str
    request: Tightening
    price: float
    """The decision price the stop must stay below."""


@dataclass(frozen=True, slots=True)
class Exit:
    symbol: str
    shares: int | None = None
    """None sells the whole position."""


type Action = RegimeView | Entry | Tranche2 | Tighten | Exit


# ---- results -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApprovedEntry:
    symbol: str
    strategy: Strategy
    pivot: float
    stop: float
    conviction: int | None
    exits: ExitPlan
    risk: float
    """The approved risk, a fraction of equity (after the max-risk and heat clips)."""
    risk_amount: float
    planned_limit: float
    """The limit with the trigger at the pivot; 09:36 recomputes it."""
    planned_shares: int
    tranches: int
    reserved_exposure: float
    """Exposure reserved for the full planned size; 09:36 sizing never exceeds it."""


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    findings: tuple[Finding, ...]
    approved: ApprovedEntry | PositionPlan | None = None
    """An approved entry, or a tightening's next plan version."""

    @property
    def outcome(self) -> Outcome:
        return outcome_of(self.findings)


@dataclass(frozen=True, slots=True)
class Evaluation:
    regime: Regime
    """The effective regime."""
    decisions: tuple[Decision, ...]


# ---- evaluation ----------------------------------------------------------------------------


@dataclass(slots=True)
class _Book:
    """Open risk, exposure and cluster members, growing with each approved entry."""

    heat: float
    exposure: float
    positions: int
    members: list[ClusterMember]
    symbols: set[str]


def _book(context: Context) -> _Book:
    holdings = list(context.holdings.values())
    return _Book(
        heat=sum(h.open_risk.risk for h in holdings) + sum(p.risk_amount for p in context.pending),
        exposure=sum(h.open_risk.exposure for h in holdings)
        + sum(p.reserved_exposure for p in context.pending),
        positions=len(holdings) + len(context.pending),
        members=[ClusterMember(h.symbol, h.industry, h.closes) for h in holdings]
        + [ClusterMember(p.symbol, p.industry, p.closes) for p in context.pending],
        symbols={h.symbol for h in holdings} | {p.symbol for p in context.pending},
    )


def regime_ceiling(regime: Regime, view: RegimeView) -> tuple[Regime, Finding | None]:
    """The regime after ``view``, which may only lower it (``regime.ladder.downgrade``)."""
    lowered = downgrade(regime, view.regime)
    if view.regime is None or lowered is view.regime:
        return lowered, None
    return lowered, clip(
        GuardrailRule.REGIME_CEILING, f"the view is above {regime}", view.regime, lowered
    )


def evaluate(actions: Sequence[Action], context: Context, risk: RiskConfig) -> Evaluation:
    """Every action, in order, after the regime views (archived README §8)."""
    regime = context.regime
    view_findings: dict[int, Finding | None] = {}
    for i, action in enumerate(actions):
        if isinstance(action, RegimeView):
            regime, view_findings[i] = regime_ceiling(regime, action)
    stepped = context.early_uptrend_stepped and regime is context.regime
    limits = regime_limits(regime, stepped=stepped, equity=context.equity, config=risk.regimes)
    book = _book(context)
    decisions: list[Decision] = []
    for i, action in enumerate(actions):
        match action:
            case RegimeView():
                finding = view_findings[i]
                decisions.append(Decision(action, (finding,) if finding else ()))
            case Entry():
                decisions.append(_entry(action, context, risk, regime, limits, book))
            case Tranche2():
                decisions.append(_tranche2(action, context, risk, regime, limits))
            case Tighten():
                decisions.append(_tighten(action, context, risk))
            case Exit():
                decisions.append(_exit(action, context))
            case _:  # pragma: no cover - every action type is handled above
                assert_never(action)
    return Evaluation(regime, tuple(decisions))


def _entry(
    a: Entry, ctx: Context, risk: RiskConfig, regime: Regime, limits: RegimeLimits, book: _Book
) -> Decision:
    g = risk.guardrails

    def rejected(finding: Finding, *earlier: Finding) -> Decision:
        return Decision(a, (*earlier, finding))

    if not finite_positive(ctx.equity, ctx.multiplier):
        return rejected(reject(GuardrailRule.INVALID_INPUT, "no valid equity or multiplier"))
    if blocked := entries_blocked(ctx.blocks):
        return rejected(blocked)
    setup = ctx.setups.get((a.symbol, a.strategy))
    if setup is None:
        return rejected(
            reject(GuardrailRule.TRADABLE_SYMBOL, f"{a.symbol} is not a {a.strategy} setup today")
        )
    if a.symbol in book.symbols:
        return rejected(
            reject(GuardrailRule.TRADABLE_SYMBOL, f"{a.symbol} is already held or pending")
        )
    if not_fundable := fundable(a.strategy, a.conviction, limits, regime):
        return rejected(not_fundable)
    exits = a.exits if a.exits is not None else risk.plans.default_exit_plan(a.strategy)
    if problems := risk.plans.menu_violations(exits):
        return rejected(reject(GuardrailRule.EXIT_PLAN, "; ".join(problems)))
    if bad := price_levels(
        pivot=a.pivot,
        stop=a.stop,
        code_pivot=setup.pivot,
        structural_stop=setup.structural_stop,
        atr=setup.atr,
        config=g,
    ):
        return rejected(bad)
    stop = a.stop
    if stop is None or setup.adr is None or not finite_positive(setup.adr, setup.price):
        return rejected(reject(GuardrailRule.INVALID_INPUT, "no ADR or price for the setup"))
    planned_limit = limit_price(
        a.pivot,
        adr=setup.adr,
        code_pivot=setup.pivot,
        max_chase=g.max_chase_above_pivot,
        config=risk.entries,
    )
    policy = risk.plans.entry[a.strategy]
    if policy.stop is StopKind.STRUCTURAL and (
        too_wide := stop_width(
            a.strategy,
            trigger=a.pivot,
            limit=planned_limit,
            stop=stop,
            atr=setup.atr,
            adr=setup.adr,
            record=ctx.records.get(a.strategy),
            config=g,
        )
    ):
        return rejected(too_wide)
    if extended := extension(a.strategy, price=setup.price, sma_50=setup.sma_50, config=g):
        return rejected(extended)
    factor = base_count_factor(setup.base_stage, setup.base_reliable, g)
    if isinstance(factor, Finding):
        return rejected(factor)
    if blackout := earnings_blackout(
        a.strategy,
        next_report_in=setup.next_report_in,
        last_report_ago=setup.last_report_ago,
        config=g,
    ):
        return rejected(blackout)

    findings: list[Finding] = []
    if a.risk is not None and not finite_positive(a.risk):
        return rejected(reject(GuardrailRule.INVALID_INPUT, "risk must be positive", a.risk))
    allowed = effective_risk(limits.base_risk, ctx.multiplier, factor, g)
    risk_fraction, clipped = risk_per_trade(a.risk, allowed)
    if clipped:
        findings.append(clipped)
    risk_amount, heat = heat_room(
        risk_fraction * ctx.equity, open_heat=book.heat, equity=ctx.equity, config=g
    )
    if heat and heat.outcome is Outcome.REJECT:
        return rejected(heat, *findings)
    if heat:
        findings.append(heat)

    member = ClusterMember(a.symbol, setup.industry, setup.closes)
    if clustered := cluster_check(member, book.members, g):
        return rejected(clustered, *findings)
    if too_many := positions_check(book.positions, limits, regime):
        return rejected(too_many, *findings)

    max_value = g.max_position_size * ctx.equity
    per_share = planned_limit - stop
    if per_share > 0 and risk_amount / per_share * planned_limit > max_value:
        findings.append(
            clip(
                GuardrailRule.POSITION_SIZE,
                f"position clipped to {g.max_position_size:.0%} of equity",
                risk_amount / per_share * planned_limit,
                max_value,
            )
        )
    shares = planned_shares(
        risk_amount=risk_amount,
        limit=planned_limit,
        stop=stop,
        max_position_value=max_value,
        exposure_room=ctx.equity,
    )
    if shares == 0:
        return rejected(
            reject(GuardrailRule.WHOLE_SHARES, "the planned size is under one share"), *findings
        )
    tranches = policy.tranches
    if tranches == 2 and shares < risk.entries.min_two_tranche_shares:
        findings.append(clip(GuardrailRule.WHOLE_SHARES, f"{shares} share(s): one tranche", 2, 1))
        tranches = 1
    reserved = shares * planned_limit
    if over := exposure_check(
        reserved, open_exposure=book.exposure, equity=ctx.equity, limits=limits
    ):
        return rejected(over, *findings)

    book.heat += risk_amount
    book.exposure += reserved
    book.positions += 1
    book.members.append(member)
    book.symbols.add(a.symbol)
    approved = ApprovedEntry(
        symbol=a.symbol,
        strategy=a.strategy,
        pivot=a.pivot,
        stop=stop,
        conviction=a.conviction,
        exits=exits,
        risk=risk_amount / ctx.equity,
        risk_amount=risk_amount,
        planned_limit=planned_limit,
        planned_shares=shares,
        tranches=tranches,
        reserved_exposure=reserved,
    )
    return Decision(a, tuple(findings), approved)


def _tranche2(
    a: Tranche2, ctx: Context, risk: RiskConfig, regime: Regime, limits: RegimeLimits
) -> Decision:
    holding = ctx.holdings.get(a.symbol)
    if holding is None:
        return Decision(a, (reject(GuardrailRule.TRADABLE_SYMBOL, f"{a.symbol} is not held"),))
    finding = (
        entries_blocked(ctx.blocks)
        or tranche2_check(
            holding.plan,
            gate=a.gate,
            session=holding.session,
            blocked_by_plan=holding.tranche2_blocked,
            config=risk.guardrails,
        )
        or fundable(holding.plan.strategy, holding.conviction, limits, regime)
        or earnings_blackout(
            holding.plan.strategy,
            next_report_in=holding.next_report_in,
            last_report_ago=holding.last_report_ago,
            config=risk.guardrails,
        )
    )
    return Decision(a, (finding,) if finding else ())


def _tighten(a: Tighten, ctx: Context, risk: RiskConfig) -> Decision:
    holding = ctx.holdings.get(a.symbol)
    if holding is None:
        return Decision(a, (reject(GuardrailRule.TRADABLE_SYMBOL, f"{a.symbol} is not held"),))
    result = tighten_check(holding.plan, a.request, price=a.price, config=risk.plans)
    if isinstance(result, Finding):
        return Decision(a, (result,))
    return Decision(a, (), result)


def _exit(a: Exit, ctx: Context) -> Decision:
    holding = ctx.holdings.get(a.symbol)
    if holding is None:
        return Decision(a, (reject(GuardrailRule.TRADABLE_SYMBOL, f"{a.symbol} is not held"),))
    finding = exit_check(a.shares, holding.shares)
    return Decision(a, (finding,) if finding else ())

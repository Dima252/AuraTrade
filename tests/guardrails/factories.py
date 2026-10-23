"""A hand-built portfolio for the guardrail tests.

Equity $10,000 in `CONFIRMED_UPTREND` (base risk 1%, multiplier 1.0). The default setup is a VCP
with code pivot 100, structural stop 93, ATR 2.5 and ADR 4%: the planned limit is 102 (2% above
the pivot), so the 8% max stop width is 8.16 below it and the default stop of 94 (8 below) fits.
$100 at risk / 8 = 12 shares, $1,224 reserved.
"""

import random
from dataclasses import replace
from typing import Any

from auratrade.config import Regime, Strategy, load_risk_config
from auratrade.guardrails.evaluate import Context, Entry, Holding, PendingEntry, SetupFacts
from auratrade.plans.entry import new_plan

RISK = load_risk_config()
G = RISK.guardrails
EQUITY = 10_000.0


def closes(seed: int, n: int = 80) -> list[float]:
    """An independent random walk: uncorrelated with every other seed's."""
    rng = random.Random(seed)  # noqa: S311 - test data, not crypto
    out = [100.0]
    for _ in range(n - 1):
        out.append(out[-1] * (1 + rng.gauss(0, 0.02)))
    return out


def setup(symbol: str = "NEW", strategy: Strategy = Strategy.VCP_BREAKOUT, **kw: Any) -> SetupFacts:
    facts = SetupFacts(
        symbol=symbol,
        strategy=strategy,
        pivot=100.0,
        structural_stop=93.0,
        price=99.0,
        atr=2.5,
        adr=0.04,
        sma_50=90.0,
        base_stage=1,
        base_reliable=True,
        industry="SEMIS",
        closes=closes(1000),
        next_report_in=30,
        last_report_ago=20,
    )
    return replace(facts, **kw)


def entry(symbol: str = "NEW", strategy: Strategy = Strategy.VCP_BREAKOUT, **kw: Any) -> Entry:
    return replace(
        Entry(symbol=symbol, strategy=strategy, pivot=100.0, stop=94.0, risk=None, conviction=3),
        **kw,
    )


def holding(
    symbol: str,
    *,
    seed: int,
    shares: int = 5,
    entry_price: float = 100.0,
    stop: float = 75.0,
    industry: str | None = None,
    strategy: Strategy = Strategy.VCP_BREAKOUT,
    **kw: Any,
) -> Holding:
    """By default $125 at risk (1.25% of equity) and $500 of exposure."""
    plan = new_plan(
        strategy, pivot=entry_price, entry_price=entry_price, initial_stop=stop, config=RISK.plans
    )
    defaults: dict[str, Any] = {
        "symbol": symbol,
        "shares": shares,
        "price": entry_price,
        "plan": plan,
        "industry": industry or f"IND-{symbol}",
        "closes": closes(seed),
        "session": 2,
        "conviction": 3,
        "last_report_ago": 20,
    }
    return Holding(**(defaults | kw))


def pending(
    symbol: str, *, seed: int, risk: float = 100.0, exposure: float = 1000.0
) -> PendingEntry:
    return PendingEntry(symbol, risk, exposure, f"IND-{symbol}", closes(seed))


def context(
    *setups: SetupFacts,
    holdings: tuple[Holding, ...] = (),
    **kw: Any,
) -> Context:
    setups = setups or (setup(),)
    defaults: dict[str, Any] = {
        "equity": EQUITY,
        "regime": Regime.CONFIRMED_UPTREND,
        "early_uptrend_stepped": False,
        "multiplier": 1.0,
        "blocks": frozenset(),
        "setups": {(s.symbol, s.strategy): s for s in setups},
        "holdings": {h.symbol: h for h in holdings},
    }
    return Context(**(defaults | kw))

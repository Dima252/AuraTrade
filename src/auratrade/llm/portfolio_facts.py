"""The facts the Portfolio Manager sees (README §5.4; #4), pure.

The jobs  gather them; this module computes what can be computed (each satellite
ETF's facts, each holding's tax if sold now, the tax budget left, the rule level, the band,
the twin) and renders them as the decision's user message:
- every number comes from code, so the LLM interprets facts and never derives a price;
- third-party text (headlines) goes in an ``<untrusted_headlines>`` block as JSON strings, with
  ``<`` and ``>`` escaped so no headline can close the block (README §9).
"""

from __future__ import annotations

import itertools
import json
import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

from auratrade.config import PortfolioConfig, PortfolioManagerConfig
from auratrade.data.news import NewsItem
from auratrade.guardrails.portfolio import PortfolioContext
from auratrade.strategies.core_allocation import (
    TRADING_DAYS,
    Trend,
    book_targets,
    rule_level,
    shift_band,
    twin_targets,
)
from auratrade.tax.israel import Lot, fifo_gain


class EventKind(StrEnum):
    """The events the Portfolio Manager runs on between rebalances (README §5.4)."""

    HALT = "HALT"  # the drawdown halt turned on
    STOP_FILLED = "STOP_FILLED"  # the leveraged fund's stop filled
    CORPORATE_ACTION = "CORPORATE_ACTION"  # on a held fund


@dataclass(frozen=True, slots=True)
class Event:
    kind: EventKind
    detail: str
    """Written by code (the order layer, the halt), never third-party text."""


@dataclass(frozen=True, slots=True)
class TrendFacts:
    state: Trend
    month_end_value: float
    """SPY's total-return value on R."""
    sma: float
    """The mean of the last ``sma_months`` month-end values."""
    volatility: float | None
    """SPY's realized volatility through R, annualized (None: too few returns, so capped)."""
    capped: bool
    halted: bool


@dataclass(frozen=True, slots=True)
class AccountFacts:
    equity: float
    peak_equity: float
    weights: Mapping[str, float]
    """The account's weights now, as fractions of equity (cash in the cash fund's name)."""
    previous_satellite: Mapping[str, float]
    """Last month's enforced satellite targets (empty before the first decision)."""
    lots: Mapping[str, Sequence[Lot]]
    prices: Mapping[str, float]
    """The decision prices (the last close before E's)."""
    realized_this_year: float
    """The account's net realized gains this calendar year (USD)."""
    loss_carried: float
    """The loss carried in from earlier years (USD, not negative)."""
    satellite_tax_this_year: float
    """The tax the satellite's sales have realized this calendar year (USD)."""


@dataclass(frozen=True, slots=True)
class EtfFacts:
    symbol: str
    returns: Mapping[str, float | None]
    """By label (``return_sessions``); None with too short a history."""
    volatility: float | None
    distance_from_sma: float | None
    correlation_with_market: float | None


@dataclass(frozen=True, slots=True)
class PastDecision:
    session: date
    core_shift: float
    satellite: Mapping[str, float] | None
    """The enforced satellite; None when it was held."""
    summary: str
    excess_vs_twin: float | None
    """The book's after-tax return less the twin's to the next decision, or to date while its
    month is open (None: not scored yet)."""
    fell_back: bool = False


@dataclass(frozen=True, slots=True)
class PmFacts:
    session: date
    """E, or the event's session."""
    ranking_session: date
    trend: TrendFacts
    account: AccountFacts
    etfs: Sequence[EtfFacts]
    regime: Mapping[str, Any] | None
    macro: Mapping[str, float]
    headlines: Sequence[NewsItem]
    history: Sequence[PastDecision]
    scorecard: Mapping[str, float] | None
    event: Event | None = None
    notes: Sequence[str] = field(default_factory=tuple)
    """Code's notes on the facts (a degraded source, a missing series)."""


def etf_facts(
    symbol: str,
    closes: Sequence[float],
    market_closes: Sequence[float],
    cfg: PortfolioManagerConfig,
) -> EtfFacts:
    """One ETF's facts from its adjusted daily closes and the market fund's on the same
    sessions (oldest first)."""
    if len(closes) != len(market_closes):
        raise ValueError(f"{symbol}: {len(closes)} closes against {len(market_closes)}")
    returns = {
        label: closes[-1] / closes[-1 - n] - 1 if len(closes) > n else None
        for label, n in cfg.return_sessions.items()
    }
    daily = _returns(closes)
    volatility = None
    if len(daily) >= cfg.volatility_sessions:
        window = daily[-cfg.volatility_sessions :]
        volatility = statistics.stdev(window) * math.sqrt(TRADING_DAYS)
    distance = None
    if len(closes) >= cfg.sma_sessions:
        distance = closes[-1] / math.fsum(closes[-cfg.sma_sessions :]) * cfg.sma_sessions - 1
    correlation = None
    n = cfg.correlation_sessions
    if len(daily) >= n:
        mine, market = daily[-n:], _returns(market_closes)[-n:]
        if statistics.pstdev(mine) > 0 and statistics.pstdev(market) > 0:
            correlation = statistics.correlation(mine, market)
    return EtfFacts(symbol, returns, volatility, distance, correlation)


def _returns(closes: Sequence[float]) -> list[float]:
    return [b / a - 1 for a, b in itertools.pairwise(closes)]


def context(facts: PmFacts, *, increases_allowed: bool) -> PortfolioContext:
    """The guardrails' view of the facts. An event only reduces risk (README §5.4), so it
    blocks increases as the kill switch does."""
    a = facts.account
    return PortfolioContext(
        state=facts.trend.state,
        capped=facts.trend.capped,
        halted=facts.trend.halted,
        increases_allowed=increases_allowed and facts.event is None,
        current=a.weights,
        previous_satellite=a.previous_satellite,
        equity=a.equity,
        lots=a.lots,
        prices=a.prices,
        satellite_tax_this_year=a.satellite_tax_this_year,
    )


def tax_budget_left(facts: PmFacts, cfg: PortfolioConfig) -> float | None:
    """The satellite's tax budget left this year (USD); None while it doesn't apply."""
    t = facts.trend
    if t.state is not Trend.UP or t.capped or t.halted:
        return None
    a = facts.account
    return max(cfg.satellite_tax_budget * a.equity - a.satellite_tax_this_year, 0.0)


def holdings(account: AccountFacts, cfg: PortfolioConfig) -> list[dict[str, Any]]:
    """Each holding's weight, value, cost, unrealized gain and the tax a full sale would
    realize now (#4). A fund without a price shows no gain (unknown), not zero."""
    rows = []
    for symbol in sorted(set(account.weights) | set(account.lots)):
        lots = account.lots.get(symbol, ())
        units = math.fsum(lot.units for lot in lots)
        cost = math.fsum(lot.cost for lot in lots)
        price = account.prices.get(symbol)
        gain = fifo_gain(lots, units, price) if price and units > 0 else None
        tax = None if gain is None else cfg.tax_rate * max(gain, 0.0)
        rows.append(
            {
                "symbol": symbol,
                "weight": _r(account.weights.get(symbol, 0.0), 4),
                "value": _r(account.weights.get(symbol, 0.0) * account.equity, 0),
                "cost": _r(cost, 0),
                "unrealized_gain": _r(gain, 0),
                "tax_if_sold_now": _r(tax, 0),
            }
        )
    return rows


def render(facts: PmFacts, cfg: PortfolioConfig) -> str:
    """The decision's user message."""
    t, a = facts.trend, facts.account
    rule = rule_level(t.state, t.capped, t.halted, cfg)
    band = shift_band(t.state, t.capped, t.halted, rule, cfg)
    neutral = book_targets(rule, {cfg.market: cfg.satellite_share}, cfg)
    budget = tax_budget_left(facts, cfg)
    rules = {
        "trend": t.state.value,
        "month_end_value": _r(t.month_end_value, 2),
        f"sma_{cfg.sma_months}m": _r(t.sma, 2),
        "distance_to_down_edge": _r(t.month_end_value / (t.sma * (1 - cfg.buffer)) - 1, 4),
        "distance_to_up_edge": _r(t.month_end_value / (t.sma * (1 + cfg.buffer)) - 1, 4),
        "volatility": _r(t.volatility, 4),
        "volatility_cap": cfg.vol_cap,
        "capped": t.capped,
        "halted": t.halted,
        "rule_level": _r(rule, 4),
        "core_shift_allowed": [_r(band.low - rule, 4), _r(band.high - rule, 4)],
        "twin_targets": _weights(twin_targets(rule, cfg)),
        "neutral_book_targets": _weights(neutral),
        "satellite_share": cfg.satellite_share,
        "max_etf_weight": cfg.max_etf_weight,
        "satellite_min_change": cfg.satellite_min_change,
        "approved_list": list(cfg.satellite_list),
    }
    account = {
        "equity": _r(a.equity, 0),
        "drawdown_from_peak": _r(1 - a.equity / a.peak_equity, 4) if a.peak_equity > 0 else None,
        "holdings": holdings(a, cfg),
        "last_month_satellite": _weights(a.previous_satellite) or None,
        "tax": {
            "rate": cfg.tax_rate,
            "realized_gains_this_year": _r(a.realized_this_year, 0),
            "loss_carried_forward": _r(a.loss_carried, 0),
            "satellite_tax_this_year": _r(a.satellite_tax_this_year, 0),
            "satellite_tax_budget_left": _r(budget, 0),
            "budget_applies": budget is not None,
        },
    }
    etfs = [
        {
            "symbol": e.symbol,
            "returns": {k: _r(v, 4) for k, v in e.returns.items()},
            "volatility": _r(e.volatility, 4),
            "distance_from_sma": _r(e.distance_from_sma, 4),
            "correlation_with_market": _r(e.correlation_with_market, 3),
        }
        for e in facts.etfs
    ]
    history = [past_decision(d) for d in facts.history]
    kind = "event" if facts.event else "rebalance"
    parts = [
        f'<decision kind="{kind}" session="{facts.session}" '
        f'ranking_session="{facts.ranking_session}"/>',
    ]
    if facts.event:
        parts.append(f'<event kind="{facts.event.kind}">{facts.event.detail}</event>')
    parts += [
        _block("rules", rules),
        _block("account", account),
        _block("satellite_etfs", etfs),
        _block("regime", facts.regime),
        _block("macro", {k: _r(v, 4) for k, v in sorted(facts.macro.items())}),
        _block("your_history", {"decisions": history, "scorecard": facts.scorecard}),
    ]
    if facts.notes:
        parts.append(_block("notes", list(facts.notes)))
    parts.append(_block("untrusted_headlines", [headline(h) for h in facts.headlines]))
    return "\n\n".join(parts)


def past_decision(d: PastDecision) -> dict[str, Any]:
    return {
        "session": str(d.session),
        "core_shift": d.core_shift,
        "satellite": _weights(d.satellite) if d.satellite is not None else "held",
        "summary": d.summary,
        "excess_vs_twin": _r(d.excess_vs_twin, 4),
        "fell_back": d.fell_back,
    }


def headline(item: NewsItem, *, summary: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "time": item.created_at.isoformat(timespec="minutes"),
        "source": item.source,
        "symbols": list(item.symbols),
        "headline": item.headline,
    }
    if summary:
        out["summary"] = item.summary
    return out


def untrusted(name: str, value: object) -> str:
    """Third-party text as data: a JSON value no string inside can break out of."""
    return _block(f"untrusted_{name}", value)


def _block(name: str, value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = text.replace("<", "\\u003c").replace(">", "\\u003e")
    return f"<{name}>{text}</{name}>"


def _weights(weights: Mapping[str, float]) -> dict[str, float]:
    return {k: round(v, 4) for k, v in sorted(weights.items()) if round(v, 4)}


def _r(value: float | None, digits: int) -> float | None:
    """Rounded; a missing or non-finite number is unknown (null), never NaN in the JSON."""
    return None if value is None or not math.isfinite(value) else round(value, digits)


def facts_to_json(facts: PmFacts) -> dict[str, Any]:
    """The facts as `portfolio_targets.facts` keeps them, without the headlines (they are
    fetched fresh on E)."""
    t, a = facts.trend, facts.account
    return {
        "session": str(facts.session),
        "ranking_session": str(facts.ranking_session),
        "trend": {
            "state": t.state.value,
            "month_end_value": t.month_end_value,
            "sma": t.sma,
            "volatility": t.volatility,
            "capped": t.capped,
            "halted": t.halted,
        },
        "account": {
            "equity": a.equity,
            "peak_equity": a.peak_equity,
            "weights": dict(a.weights),
            "previous_satellite": dict(a.previous_satellite),
            "lots": {
                s: [[str(lot.opened), lot.units, lot.cost] for lot in lots]
                for s, lots in a.lots.items()
            },
            "prices": dict(a.prices),
            "realized_this_year": a.realized_this_year,
            "loss_carried": a.loss_carried,
            "satellite_tax_this_year": a.satellite_tax_this_year,
        },
        "etfs": [
            {
                "symbol": e.symbol,
                "returns": dict(e.returns),
                "volatility": e.volatility,
                "distance_from_sma": e.distance_from_sma,
                "correlation_with_market": e.correlation_with_market,
            }
            for e in facts.etfs
        ],
        "regime": facts.regime,
        "macro": dict(facts.macro),
        "history": [
            {
                "session": str(d.session),
                "core_shift": d.core_shift,
                "satellite": None if d.satellite is None else dict(d.satellite),
                "summary": d.summary,
                "excess_vs_twin": d.excess_vs_twin,
                "fell_back": d.fell_back,
            }
            for d in facts.history
        ],
        "scorecard": facts.scorecard,
        "event": None if facts.event is None else [facts.event.kind.value, facts.event.detail],
        "notes": list(facts.notes),
    }


def facts_from_json(data: Mapping[str, Any]) -> PmFacts:
    t, a = data["trend"], data["account"]
    event = data.get("event")
    return PmFacts(
        session=date.fromisoformat(data["session"]),
        ranking_session=date.fromisoformat(data["ranking_session"]),
        trend=TrendFacts(
            Trend(t["state"]),
            t["month_end_value"],
            t["sma"],
            t["volatility"],
            t["capped"],
            t["halted"],
        ),
        account=AccountFacts(
            equity=a["equity"],
            peak_equity=a["peak_equity"],
            weights=a["weights"],
            previous_satellite=a["previous_satellite"],
            lots={
                s: [Lot(date.fromisoformat(d), units, cost) for d, units, cost in lots]
                for s, lots in a["lots"].items()
            },
            prices=a["prices"],
            realized_this_year=a["realized_this_year"],
            loss_carried=a["loss_carried"],
            satellite_tax_this_year=a["satellite_tax_this_year"],
        ),
        etfs=[
            EtfFacts(
                e["symbol"],
                e["returns"],
                e["volatility"],
                e["distance_from_sma"],
                e["correlation_with_market"],
            )
            for e in data["etfs"]
        ],
        regime=data["regime"],
        macro=data["macro"],
        headlines=(),
        history=[
            PastDecision(
                date.fromisoformat(d["session"]),
                d["core_shift"],
                d["satellite"],
                d["summary"],
                d["excess_vs_twin"],
                d["fell_back"],
            )
            for d in data["history"]
        ],
        scorecard=data["scorecard"],
        event=None if event is None else Event(EventKind(event[0]), event[1]),
        notes=tuple(data["notes"]),
    )

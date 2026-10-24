"""The shadow baseline book (journal.baseline)."""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from auratrade.config import Regime, Strategy
from auratrade.guardrails.levels import StrategyRecord
from auratrade.guardrails.model import GuardrailRule, Outcome
from auratrade.journal.baseline import (
    BookTrade,
    Listed,
    convictions,
    decide,
    equity_on,
    multiplier,
    proposals,
    records,
)
from auratrade.plans.model import Rule
from auratrade.sim.trade import FillKind, TradeFill, TradeOutcome, TradeResult
from tests.guardrails.factories import RISK, closes, context, setup

VCP, PULLBACK = Strategy.VCP_BREAKOUT, Strategy.LEADER_PULLBACK
DAYS = [date(2026, 10, 1) + timedelta(days=i) for i in range(30)]


def listed(n: int, **kw: object) -> list[Listed]:
    return [
        Listed(
            setup(f"S{i}", structural_stop=94.0, industry=f"IND{i}", closes=closes(i), **kw),  # type: ignore[arg-type]
            score=0.9 - i / 100,
        )
        for i in range(n)
    ]


class TestDecide:
    def test_the_top_quarter_by_score_gets_conviction_4(self) -> None:
        items = [Listed(setup(f"S{i}"), score=s) for i, s in enumerate([0.5, 0.9, 0.7, 0.6, 0.8])]
        assert convictions(items) == [3, 4, 3, 3, 4]
        assert convictions([]) == []

    def test_proposals_take_the_code_levels_and_default_risk(self) -> None:
        [entry] = proposals(listed(1))
        assert (entry.pivot, entry.stop, entry.risk, entry.exits) == (100.0, 94.0, None, None)

    def test_it_takes_setups_in_rank_order_until_the_limits_are_full(self) -> None:
        items = listed(10)
        ctx = context(*(i.facts for i in items))
        result = decide(items, ctx, RISK)
        approved = [d.approved.symbol for d in result.decisions if d.approved]  # type: ignore[union-attr]
        assert approved == [f"S{i}" for i in range(6)]  # 6 x 1% fills the 6% heat cap
        assert {d.findings[-1].rule for d in result.decisions[6:]} == {GuardrailRule.HEAT}

    def test_under_pressure_funds_only_the_top_quarter(self) -> None:
        items = listed(8)
        ctx = context(*(i.facts for i in items), regime=Regime.UNDER_PRESSURE)
        result = decide(items, ctx, RISK)
        outcomes = [d.outcome for d in result.decisions]
        assert outcomes[:2] == [Outcome.APPROVE, Outcome.APPROVE]
        assert {d.findings[-1].rule for d in result.decisions[2:]} == {GuardrailRule.FUNDABLE}

    def test_nothing_in_correction(self) -> None:
        items = listed(3)
        result = decide(items, context(*(i.facts for i in items), regime=Regime.CORRECTION), RISK)
        assert all(d.approved is None for d in result.decisions)


def trade(
    strategy: Strategy,
    entry: int,
    marks: tuple[float, ...],
    *,
    closed: bool,
    risk: float = 100.0,
    cost: float = 1000.0,
) -> BookTrade:
    result = TradeResult(
        outcome=TradeOutcome.CLOSED if closed else TradeOutcome.OPEN,
        fills=(TradeFill(1, FillKind.ENTRY, 10, cost / 10),),
        exit=Rule.TRAIL if closed else None,
        risk=risk,
        pnl=marks[-1] * risk,
        marks=marks,
    )
    return BookTrade(strategy, DAYS[entry:], result)


class TestAccount:
    def test_equity_marks_open_trades_and_books_closed_ones(self) -> None:
        trades = [
            trade(VCP, 2, (0.5, 1.0, 2.0), closed=True),  # exits on DAYS[4] at +2R
            trade(PULLBACK, 3, (-0.5, -0.2), closed=False),
        ]
        assert equity_on(5000, trades, DAYS[1]) == 5000
        assert equity_on(5000, trades, DAYS[3]) == pytest.approx(5000 + 100 - 50)
        assert equity_on(5000, trades, DAYS[4]) == pytest.approx(5000 + 200 - 20)
        assert equity_on(5000, trades, DAYS[20]) == pytest.approx(5000 + 200 - 20)
        assert trades[0].exit_session == DAYS[4]
        assert trades[1].exit_session is None

    def test_a_missed_entry_has_no_pnl(self) -> None:
        missed = BookTrade(VCP, DAYS[2:], TradeResult(TradeOutcome.MISSED))
        assert (missed.pnl_at(DAYS[5]), missed.cost, missed.closed) == (0.0, 0.0, False)

    def test_the_multiplier_starts_at_prove_it_and_follows_the_books_trades(self) -> None:
        assert multiplier(5000, [], [], RISK) == RISK.multiplier.prove_it_multiplier
        assert multiplier(5000, [], DAYS[:3], RISK) == RISK.multiplier.prove_it_multiplier
        wins = [trade(VCP, i, (1.0, 1.0), closed=True, risk=10) for i in (0, 2)]
        assert multiplier(5000, wins, DAYS[:6], RISK) == 1.0  # 2 wins end "prove it first"

    def test_records_feed_keep_losses_small(self) -> None:
        trades = [
            trade(VCP, 0, (0.5, 2.0), closed=True, cost=1000),  # +$200 on $1,000: +20%
            trade(VCP, 1, (-1.0,), closed=True, cost=1000),
            trade(PULLBACK, 1, (1.0,), closed=False),
        ]
        out = records(trades, RISK)
        assert out[VCP] == StrategyRecord(2, pytest.approx(0.2))
        assert out[PULLBACK] == StrategyRecord(0, None)

    def test_a_trade_without_cost_is_left_out_of_the_records(self) -> None:
        free = trade(VCP, 0, (1.0,), closed=True)
        free = replace(free, result=replace(free.result, fills=()))
        assert records([free], RISK)[VCP] == StrategyRecord(0, None)

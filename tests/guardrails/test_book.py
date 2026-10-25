import pytest

from auratrade.config import BookConfig, BookOverlay, GuardrailsConfig, Regime, load_risk_config
from auratrade.guardrails.blocks import EntryBlock
from auratrade.guardrails.book import (
    BookAccount,
    BookBuy,
    BuyDecision,
    book_halts,
    evaluate_buys,
    evaluate_exit,
    evaluate_stop_raise,
    overlay_blocks,
)
from auratrade.guardrails.clusters import ClusterMember
from auratrade.guardrails.model import GuardrailRule, Outcome

RISK = load_risk_config()
CFG: BookConfig = RISK.book
GUARD: GuardrailsConfig = RISK.guardrails.model_copy(
    update={"cluster_correlation_sessions": 4, "cluster_correlation": 0.99}
)
SLIP = 0.005

PATTERNS = {
    "A": [10, 11, 10, 11, 10],
    "B": [10, 10, 11, 10, 11],
    "C": [10, 12, 11, 11, 13],
    "D": [10, 9, 10, 12, 11],
    "E": [10, 10, 10, 11, 12],
}


def member(symbol: str, industry: str | None = None) -> ClusterMember:
    return ClusterMember(symbol, industry, [float(c) for c in PATTERNS[symbol]])


def account(
    *,
    cash: float = 5000.0,
    exposure: float = 0.0,
    held: tuple[str, ...] = (),
    pending: tuple[str, ...] = (),
    stops: dict[str, float] | None = None,
) -> BookAccount:
    return BookAccount(5000.0, cash, exposure, frozenset(held), frozenset(pending), stops or {})


def buys(
    *orders: BookBuy,
    acct: BookAccount | None = None,
    prices: dict[str, float | None] | None = None,
    blocks: frozenset[EntryBlock] = frozenset(),
    holdings: list[ClusterMember] | None = None,
    members: dict[str, ClusterMember] | None = None,
    cfg: BookConfig = CFG,
    clusters: bool = True,
) -> list[BuyDecision]:
    return evaluate_buys(
        list(orders),
        candidates=["C", "D", "E"],
        account=acct or account(),
        prices=prices if prices is not None else {"C": 100.0, "D": 50.0, "E": 25.0},
        blocks=blocks,
        holdings=holdings or [],
        members=members
        if members is not None
        else {"C": member("C", "tech"), "D": member("D", "energy"), "E": member("E", "retail")},
        cfg=cfg,
        guard=GUARD,
        limit_slippage=SLIP,
        clusters=clusters,
    )


def rules(decisions: list[BuyDecision]) -> list[GuardrailRule | None]:
    return [d.findings[0].rule if d.findings else None for d in decisions]


class TestBlocks:
    def test_book_drawdown_threshold(self) -> None:
        halts = RISK.halts
        quiet = book_halts(day_return=0.0, week_return=0.0, drawdown=0.25, halts=halts, cfg=CFG)
        assert quiet == frozenset()  # the setups' 15% would have halted
        deep = book_halts(day_return=-0.02, week_return=0.0, drawdown=0.30, halts=halts, cfg=CFG)
        assert deep == {EntryBlock.DAILY_LOSS_HALT, EntryBlock.DRAWDOWN_HALT}

    @pytest.mark.parametrize(
        ("overlay", "regime", "crisis", "spy", "expected"),
        [
            (BookOverlay.OFF, Regime.CORRECTION, True, False, set()),
            (BookOverlay.REGIME, Regime.UNDER_PRESSURE, False, None, set()),
            (BookOverlay.REGIME, Regime.CORRECTION, False, None, {EntryBlock.CORRECTION}),
            (BookOverlay.REGIME, None, False, None, {EntryBlock.CORRECTION}),
            (
                BookOverlay.REGIME,
                Regime.CONFIRMED_UPTREND,
                True,
                None,
                {EntryBlock.CRISIS_OVERRIDE},
            ),
            (BookOverlay.SPY_SMA, Regime.CORRECTION, False, True, set()),
            (BookOverlay.SPY_SMA, None, False, False, {EntryBlock.INDEX_TREND}),
            (BookOverlay.SPY_SMA, None, False, None, {EntryBlock.INDEX_TREND}),
            (BookOverlay.SPY_SMA, None, True, True, {EntryBlock.CRISIS_OVERRIDE}),
        ],
    )
    def test_overlay(
        self,
        overlay: BookOverlay,
        regime: Regime | None,
        crisis: bool,
        spy: bool | None,
        expected: set[EntryBlock],
    ) -> None:
        cfg = CFG.model_copy(update={"overlay": overlay})
        assert (
            overlay_blocks(cfg, effective_regime=regime, crisis=crisis, spy_above_sma=spy)
            == expected
        )


class TestBuys:
    def test_approved_full_and_half_slots(self) -> None:
        out = buys(BookBuy("C"), BookBuy("D", 0.5))
        # C: 625 / 100.50 -> 6 shares, stop 80.00; D: 312.50 / 50.25 -> 6 shares, stop 40.00
        assert out == [
            BuyDecision("C", Outcome.APPROVE, (), 6, 100.5, 80.0),
            BuyDecision("D", Outcome.APPROVE, (), 6, 50.25, 40.0),
        ]

    def test_blocks_reject_every_buy(self) -> None:
        out = buys(BookBuy("C"), BookBuy("D"), blocks=frozenset({EntryBlock.KILL_SWITCH}))
        assert rules(out) == [GuardrailRule.ENTRIES_BLOCKED] * 2
        assert all(d.outcome is Outcome.REJECT and d.shares == 0 for d in out)

    def test_rejections_in_order(self) -> None:
        out = buys(
            BookBuy("C", 0.75),  # not 1 or 0.5
            BookBuy("ZZZ"),  # not a candidate
            BookBuy("D"),  # held
            BookBuy("E"),  # pending
            acct=account(held=("D",), pending=("E",)),
        )
        assert rules(out) == [
            GuardrailRule.INVALID_INPUT,
            GuardrailRule.TRADABLE_SYMBOL,
            GuardrailRule.TIGHTEN_ONLY,
            GuardrailRule.TIGHTEN_ONLY,
        ]

    def test_no_buying_the_same_name_twice_and_max_positions(self) -> None:
        held = tuple(f"H{i}" for i in range(CFG.holdings - 1))
        out = buys(BookBuy("C"), BookBuy("C"), BookBuy("D"), acct=account(held=held))
        assert rules(out) == [None, GuardrailRule.TIGHTEN_ONLY, GuardrailRule.MAX_POSITIONS]

    def test_unusable_inputs(self) -> None:
        out = buys(BookBuy("C"), prices={"C": None})
        assert rules(out) == [GuardrailRule.INVALID_INPUT]
        out = buys(BookBuy("C"), acct=account(cash=float("nan")))
        assert rules(out) == [GuardrailRule.INVALID_INPUT]

    def test_clusters(self) -> None:
        holdings = [member("A", "tech"), member("B", "tech")]
        out = buys(
            BookBuy("C"),
            BookBuy("D"),
            BookBuy("E"),
            holdings=holdings,
            members={"C": member("C", "tech"), "D": member("D", "energy")},
        )
        assert rules(out) == [GuardrailRule.CLUSTER, None, GuardrailRule.CLUSTER]
        assert "no cluster data" in out[2].findings[0].message

    def test_an_approval_counts_against_the_next_cluster_check(self) -> None:
        out = buys(
            BookBuy("C"),
            BookBuy("D"),
            holdings=[member("A", "tech")],
            members={"C": member("C", "tech"), "D": member("D", "tech")},
        )
        assert rules(out) == [None, GuardrailRule.CLUSTER]

    def test_clusters_off_without_data(self) -> None:
        out = buys(BookBuy("C"), BookBuy("D"), members={}, clusters=False)
        assert [d.outcome for d in out] == [Outcome.APPROVE, Outcome.APPROVE]

    def test_clipped_to_cash_then_rejected_at_zero(self) -> None:
        out = buys(BookBuy("C"), BookBuy("D"), acct=account(cash=330.0))
        # C: 330 / 100.50 -> 3 of 6 shares (301.50 spent); D: 28.50 left -> 0 shares
        assert out[0].outcome is Outcome.CLIP
        assert (out[0].shares, out[0].findings[0].proposed, out[0].findings[0].enforced) == (
            3,
            6,
            3,
        )
        assert rules(out) == [GuardrailRule.NO_MARGIN, GuardrailRule.NO_MARGIN]
        assert out[1].outcome is Outcome.REJECT

    def test_exposure_cap(self) -> None:
        # 4,800 already invested: 200 of room under 100% of 5,000, though the cash is there.
        out = buys(BookBuy("E"), acct=account(exposure=4800.0))
        assert (out[0].outcome, out[0].shares) == (Outcome.CLIP, 7)  # 200 / 25.13


class TestExitsAndStops:
    def test_exit(self) -> None:
        acct = account(held=("C",))
        assert evaluate_exit("C", acct) is None
        finding = evaluate_exit("D", acct)
        assert finding is not None
        assert finding.rule is GuardrailRule.TRADABLE_SYMBOL

    def test_stop_raise_rounds_down_and_approves(self) -> None:
        out = evaluate_stop_raise(
            "C", 96.129, account=account(held=("C",), stops={"C": 80.0}), last_price=110.0
        )
        assert (out.finding, out.stop) == (None, 96.12)

    @pytest.mark.parametrize(
        ("symbol", "stop", "stops", "last", "rule"),
        [
            ("D", 90.0, {"C": 80.0}, 110.0, GuardrailRule.TRADABLE_SYMBOL),
            ("C", 90.0, {}, 110.0, GuardrailRule.INVALID_INPUT),
            ("C", float("nan"), {"C": 80.0}, 110.0, GuardrailRule.INVALID_INPUT),
            ("C", 90.0, {"C": 80.0}, None, GuardrailRule.INVALID_INPUT),
            ("C", 80.009, {"C": 80.0}, 110.0, GuardrailRule.TIGHTEN_ONLY),  # rounds to 80.00
            ("C", 70.0, {"C": 80.0}, 110.0, GuardrailRule.TIGHTEN_ONLY),
            ("C", 110.0, {"C": 80.0}, 110.0, GuardrailRule.PRICE_LEVELS),
        ],
    )
    def test_stop_raise_rejections(
        self,
        symbol: str,
        stop: float,
        stops: dict[str, float],
        last: float | None,
        rule: GuardrailRule,
    ) -> None:
        out = evaluate_stop_raise(
            symbol, stop, account=account(held=("C",), stops=stops), last_price=last
        )
        assert out.stop is None
        assert out.finding is not None
        assert out.finding.rule is rule

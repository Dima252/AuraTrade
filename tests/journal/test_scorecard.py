"""The Analyst verdict scorecard (journal.scorecard), on synthetic verdicts."""

from datetime import date, timedelta

import pytest

from auratrade.config import Strategy, load_risk_config
from auratrade.journal.scorecard import (
    Interval,
    Observation,
    Review,
    Verdict,
    block_bootstrap,
    by_tag,
    by_verdict,
    compare,
    in_force,
    observation,
    summarize,
    units,
)

CARD = load_risk_config().scorecard
MAX_AGE = load_risk_config().ranking.verdict_max_age_sessions  # 3
VCP = Strategy.VCP_BREAKOUT
DAYS = [date(2026, 9, 1) + timedelta(days=i) for i in range(60)]
POS = {d: i for i, d in enumerate(DAYS)}
STATS = {"samples": 500, "confidence": 0.9, "seed": CARD.seed}


def review(
    day: int,
    verdict: Verdict = Verdict.BUY,
    symbol: str = "AAA",
    structure: float = 50.0,
    prompt: str = "v1",
    model: str = "claude-opus-5-5",
) -> Review:
    return Review(symbol, VCP, structure, DAYS[day], verdict, 4, prompt, model)


class TestUnits:
    def test_re_reviewing_a_setup_five_times_is_one_observation(self) -> None:
        reviews = [review(d, Verdict.SKIP if d < 12 else Verdict.BUY) for d in (0, 4, 8, 12, 16)]
        grouped = units(reversed(reviews))
        assert list(grouped) == [("AAA", VCP, 50.0)]
        [unit] = grouped.values()
        assert [r.session for r in unit] == [DAYS[d] for d in (0, 4, 8, 12, 16)]
        obs = observation(unit, trigger_session=DAYS[17], position=POS, max_age=MAX_AGE)
        assert obs.verdict is Verdict.BUY
        assert obs.label.session == DAYS[16]

    def test_a_new_structure_is_a_new_unit(self) -> None:
        grouped = units([review(0), review(1, structure=52.0), review(1, symbol="BBB")])
        assert len(grouped) == 3


class TestInForce:
    def test_a_verdict_governs_the_sessions_its_cache_entry_lasts(self) -> None:
        reviews = [review(10)]
        # Cached through the evening 3 sessions after the review: trading sessions 11-14.
        assert in_force(reviews, DAYS[10], POS, MAX_AGE) is None
        for day in (11, 12, 13, 14):
            assert in_force(reviews, DAYS[day], POS, MAX_AGE) == reviews[0]
        assert in_force(reviews, DAYS[15], POS, MAX_AGE) is None

    def test_the_latest_earlier_review_governs(self) -> None:
        reviews = [review(10, Verdict.SKIP), review(12, Verdict.BUY)]
        assert in_force(reviews, DAYS[12], POS, MAX_AGE) == reviews[0]
        assert in_force(reviews, DAYS[13], POS, MAX_AGE) == reviews[1]

    def test_no_review_before_the_session(self) -> None:
        assert in_force([review(10)], DAYS[5], POS, MAX_AGE) is None


class TestObservation:
    def test_an_untriggered_unit_takes_its_last_verdict(self) -> None:
        reviews = [review(0, Verdict.BUY), review(4, Verdict.WATCH)]
        obs = observation(reviews, trigger_session=None, position=POS, max_age=MAX_AGE)
        assert (obs.triggered, obs.verdict, obs.trigger_session) == (False, Verdict.WATCH, None)

    def test_a_trigger_outside_every_verdict_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="no verdict in force"):
            observation([review(0)], trigger_session=DAYS[9], position=POS, max_age=MAX_AGE)
        with pytest.raises(ValueError, match="at least one review"):
            observation([], trigger_session=None, position=POS, max_age=MAX_AGE)

    def test_tagged_with_the_labelling_verdicts_prompt_and_model(self) -> None:
        reviews = [review(0, prompt="v1"), review(4, prompt="v2", model="fallback")]
        obs = observation(reviews, trigger_session=DAYS[2], position=POS, max_age=MAX_AGE)
        assert (obs.label.prompt_version, obs.label.model) == ("v1", "claude-opus-5-5")
        assert list(by_tag([obs])) == [("v1", "claude-opus-5-5")]


def obs(verdict: Verdict, day: int | None, r5: float | None, symbol: str = "AAA") -> Observation:
    label = review(0 if day is None else day - 1, verdict, symbol=symbol)
    trigger = None if day is None else DAYS[day]
    return Observation(label.unit, label, day is not None, trigger, {5: r5})


class TestStatistics:
    def test_summary_counts_setups_triggers_and_sessions(self) -> None:
        group = [
            obs(Verdict.BUY, 5, 2.0, "A"),
            obs(Verdict.BUY, 5, -1.0, "B"),
            obs(Verdict.BUY, 9, 1.0, "C"),
            obs(Verdict.BUY, 12, None, "D"),  # triggered, R not reached yet
            obs(Verdict.BUY, None, None, "E"),
        ]
        s = summarize(group, 5, **STATS)
        assert (s.setups, s.triggered, s.scored, s.sessions) == (5, 4, 3, 2)
        assert s.trigger_rate == pytest.approx(0.8)
        assert s.expectancy == pytest.approx(2 / 3)
        assert s.interval is not None
        assert s.interval.low <= s.expectancy <= s.interval.high

    def test_an_empty_group_has_no_figures(self) -> None:
        s = summarize([], 5, **STATS)
        assert (s.setups, s.trigger_rate, s.expectancy, s.interval) == (0, None, None, None)

    def test_buy_vs_skip(self) -> None:
        buys = [obs(Verdict.BUY, d, 1.0 + d % 3, f"B{d}") for d in range(5, 35)]
        skips = [obs(Verdict.SKIP, d, -0.5 + d % 2, f"S{d}") for d in range(5, 35)]
        c = compare(buys, skips, 5, **STATS)
        assert c.difference == pytest.approx(c.a.expectancy - c.b.expectancy)  # type: ignore[operator]
        assert c.interval is not None
        assert c.interval.low > 0
        assert c.sessions == 30

    def test_no_difference_without_both_groups(self) -> None:
        c = compare([obs(Verdict.BUY, 5, 1.0)], [], 5, **STATS)
        assert (c.difference, c.interval) == (None, None)

    def test_the_bootstrap_resamples_whole_sessions(self) -> None:
        # One session holds 10 units at +1R, another 1 unit at -1R. Resampling units would put
        # the interval near +0.8R; resampling sessions puts weight on all-(-1R) draws.
        group = {DAYS[0]: [1.0] * 10, DAYS[1]: [-1.0]}
        interval = block_bootstrap(
            [group],
            lambda g: sum(g[0]) / len(g[0]) if g[0] else None,
            samples=400,
            confidence=0.9,
            seed=1,
        )
        assert interval == Interval(-1.0, 1.0)

    def test_the_bootstrap_is_seeded(self) -> None:
        group = {DAYS[i]: [float(i % 4 - 1)] for i in range(20)}
        run = lambda: block_bootstrap(  # noqa: E731
            [group], lambda g: sum(g[0]) / len(g[0]), samples=200, confidence=0.9, seed=5
        )
        assert run() == run()

    def test_no_sessions_or_no_defined_statistic(self) -> None:
        assert block_bootstrap([{}], lambda g: 0.0, samples=10, confidence=0.9, seed=1) is None
        group = {DAYS[0]: [1.0]}
        assert block_bootstrap([group], lambda g: None, samples=10, confidence=0.9, seed=1) is None

    def test_by_verdict_keeps_every_verdict(self) -> None:
        grouped = by_verdict([obs(Verdict.SKIP, 5, 1.0)])
        assert [len(v) for v in grouped.values()] == [0, 0, 1]

"""Minervini's Trend Template: the 8-rule stage-2 screen in front of the scans (README §5.3).

Computed for every session of a symbol's history in one pass, so backtests read the same series
the evening scan reads at its last session. A rule whose inputs are still warming up (fewer than
200 + 22 sessions for rule 3, 252 for the 52-week range, no RS rating) fails: missing data never
passes a screen.

Rule 8 is cross-sectional: the RS rating is the percentile of the RS score across the universe
on the same session, so the caller ranks the universe (``rs_ratings``) and passes each symbol's
ratings in.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, fields

from auratrade.config import Strategy, TrendTemplateConfig
from auratrade.indicators.bars import OHLCV
from auratrade.indicators.moving_averages import rising, sma
from auratrade.indicators.ranges import highest_high, lowest_low
from auratrade.indicators.relative_strength import percentile_ranks, rs_score
from auratrade.indicators.series import check_same_length


@dataclass(frozen=True, slots=True)
class TrendTemplate:
    """The 8 rules at one session, in README order (rule 1 is the first field)."""

    above_mid_and_long: bool
    """1. Price > 150 SMA and > 200 SMA."""
    mid_above_long: bool
    """2. 150 SMA > 200 SMA."""
    long_rising: bool
    """3. 200 SMA rising for at least 22 sessions."""
    short_above_mid_and_long: bool
    """4. 50 SMA > 150 SMA and > 200 SMA."""
    above_short: bool
    """5. Price > 50 SMA."""
    above_low: bool
    """6. Price >= 30% above the 52-week low."""
    near_high: bool
    """7. Price within 25% of the 52-week high."""
    strong_rs: bool
    """8. RS rating >= 70."""

    def rule(self, number: int) -> bool:
        if not 1 <= number <= 8:
            raise ValueError(f"Trend Template rules are numbered 1-8, got {number}")
        result: bool = getattr(self, fields(self)[number - 1].name)
        return result

    @property
    def failed(self) -> tuple[int, ...]:
        """The numbers of the rules that failed."""
        return tuple(n for n in range(1, 9) if not self.rule(n))

    def passes(self, rules: Collection[int] = range(1, 9)) -> bool:
        """True when every rule in ``rules`` passes (all 8 by default)."""
        return all(self.rule(n) for n in rules)


def _gt(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and a > b


def trend_template(
    bars: Sequence[OHLCV],
    rs_ratings: Sequence[float | None],
    config: TrendTemplateConfig,
) -> list[TrendTemplate]:
    """The Trend Template for every session of ``bars``.

    ``rs_ratings`` is aligned with ``bars``: the symbol's RS rating (0-100) on each session, or
    ``None`` where it wasn't ranked.
    """
    check_same_length(bars, rs_ratings)
    closes = [bar.close for bar in bars]
    short = sma(closes, config.short_sma)
    mid = sma(closes, config.mid_sma)
    long = sma(closes, config.long_sma)
    long_rising = rising(long, config.long_sma_rising_sessions)
    lows = lowest_low(bars, config.range_sessions)
    highs = highest_high(bars, config.range_sessions)

    out: list[TrendTemplate] = []
    for t, close in enumerate(closes):
        low, high, rating = lows[t], highs[t], rs_ratings[t]
        out.append(
            TrendTemplate(
                above_mid_and_long=_gt(close, mid[t]) and _gt(close, long[t]),
                mid_above_long=_gt(mid[t], long[t]),
                long_rising=long_rising[t],
                short_above_mid_and_long=_gt(short[t], mid[t]) and _gt(short[t], long[t]),
                above_short=_gt(close, short[t]),
                above_low=low is not None and close >= low * (1 + config.min_above_low),
                near_high=high is not None and close >= high * (1 - config.max_below_high),
                strong_rs=rating is not None and rating >= config.min_rs_rating,
            )
        )
    return out


def rs_scores(closes: Sequence[float], config: TrendTemplateConfig) -> list[float | None]:
    """The RS score behind rule 8 for every session: ``0.4*R63 + 0.2*R126 + ...``."""
    return rs_score(closes, [(w.lookback, w.weight) for w in config.rs_weights])


def rs_ratings(
    closes_by_symbol: Mapping[str, Sequence[float]], config: TrendTemplateConfig
) -> dict[str, float]:
    """RS ratings for the session every close series ends on: the percentile of the RS score.

    A symbol without enough history for the longest lookback is left out (so it fails rule 8)
    rather than ranked on a shorter return.
    """
    scores: dict[str, float] = {}
    for symbol, closes in closes_by_symbol.items():
        score = rs_scores(closes, config)[-1] if closes else None
        if score is not None:
            scores[symbol] = score
    return percentile_ranks(scores) if scores else {}


def passes_for(template: TrendTemplate, strategy: Strategy, config: TrendTemplateConfig) -> bool:
    """Whether the stock passes the rules ``strategy`` needs (all 8, rules 1/5-8 for emerging
    leaders, none for episodic pivots)."""
    return template.passes(config.required_rules[strategy])

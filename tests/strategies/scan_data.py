"""Hand-built histories for the strategy scans, with the answers worked out in the comments."""

from collections.abc import Sequence

from auratrade.config import RiskConfig, load_risk_config
from auratrade.indicators.bars import Bar
from auratrade.strategies.base import SymbolData
from auratrade.strategies.trend_template import TrendTemplate

RISK = load_risk_config()

PASSING = TrendTemplate(*([True] * 8))
FAILING = TrendTemplate(*([False] * 8))


def bar(high: float, low: float, *, close: float | None = None, volume: float = 1000) -> Bar:
    mid = (high + low) / 2 if close is None else close
    return Bar(open=mid, high=high, low=low, close=mid, volume=volume)


def data(
    bars: Sequence[Bar],
    *,
    template: TrendTemplate = PASSING,
    rs: float | None = 90.0,
    momentum: float | None = 99.0,
    earnings: frozenset[int] = frozenset(),
) -> SymbolData:
    return SymbolData(
        symbol="TEST",
        bars=list(bars),
        templates=[template] * len(bars),
        rs_ratings=[rs] * len(bars),
        momentum_ranks=[momentum] * len(bars),
        earnings_sessions=earnings,
    )


def with_strategy(risk: RiskConfig, strategy: str, **changes: object) -> RiskConfig:
    """``risk`` with some of one strategy's scan settings changed."""
    section = getattr(risk.strategies, strategy).model_copy(update=changes)
    strategies = risk.strategies.model_copy(update={strategy: section})
    return risk.model_copy(update={"strategies": strategies})


# --- a textbook VCP: 25% -> 12% -> 6% -------------------------------------------------------
# 50 rising bars (highs 60..99), the base high 100 at session 50, then three contractions:
# 100 -> 75 (25%), 98 -> 86.24 (12%), 97 -> 91.18 (6%), and a rise to 95.5 at T (session 74).
# The final contraction's 6 sessions after its high trade 500 shares against 1,000 before.
PRE_BASE = [bar(60 + i * 39 / 49, 59 + i * 39 / 49) for i in range(50)]
BASE_HIGH = [bar(100, 98)]
FIRST = [bar(h, lo) for h, lo in [(97, 94), (92, 89), (87, 84), (82, 79), (78, 75)]]
FIRST_RALLY = [bar(h, lo) for h, lo in [(80, 77), (85, 82), (90, 87), (95, 92), (98, 95)]]
SECOND = [bar(h, lo) for h, lo in [(96, 93), (93, 90), (90, 87), (88, 86.24)]]
SECOND_RALLY = [bar(h, lo) for h, lo in [(89, 87), (92, 89), (95, 92), (97, 94)]]
THIRD = [bar(h, lo, volume=500) for h, lo in [(96, 94), (94, 92.5), (93, 91.18)]]
FINAL_RISE = [bar(h, lo, volume=500) for h, lo in [(93, 92), (94.5, 93), (95.5, 94)]]


def vcp_bars(third: Sequence[Bar] = THIRD, final_rise: Sequence[Bar] = FINAL_RISE) -> list[Bar]:
    return [
        *PRE_BASE,
        *BASE_HIGH,
        *FIRST,
        *FIRST_RALLY,
        *SECOND,
        *SECOND_RALLY,
        *third,
        *final_rise,
    ]


# --- a momentum breakout: +60% in 20 sessions, then a 12-session flag ----------------------
# 30 flat bars at 50, a move to the peak high 80 at session 49 (+61.6% from the low 49.5), then
# 12 sessions (T = 61): the first 6 with lows from 72 and ~5.4% ranges, the last 6 with lows
# from 75 and ~2.3% ranges, closing 77.5 above the rising 20-day (75.12, from 72.06).
FLAT = [bar(50.5, 49.5) for _ in range(30)]
MOVE = [bar(51.5 + i * 1.5, 50.5 + i * 1.5) for i in range(19)] + [bar(80, 78)]
FLAG_FIRST = [
    bar(h, lo) for h, lo in [(78, 74), (77, 73), (76.5, 72), (76, 72.5), (77, 73), (76.5, 73)]
]
FLAG_SECOND = [
    bar(h, lo) for h, lo in [(77, 75), (77.5, 75.5), (77, 75.2), (77.8, 76), (77.6, 75.8), (78, 77)]
]


def momentum_bars(
    first: Sequence[Bar] = FLAG_FIRST, second: Sequence[Bar] = FLAG_SECOND
) -> list[Bar]:
    return [*FLAT, *MOVE, *first, *second]


# --- an episodic pivot: 60 quiet sessions at 20, then a 12.5% gap on 4x volume ----------------
QUIET = [Bar(open=20, high=20.2, low=19.8, close=20, volume=1000) for _ in range(60)]
GAP = Bar(open=22.5, high=24, low=22, close=23.5, volume=4000)


def line(closes: Sequence[float], *, volume: float = 1000) -> list[Bar]:
    """Bars closing at ``closes`` with a range of 1 around each close."""
    return [bar(c + 0.5, c - 0.5, close=c, volume=volume) for c in closes]


# --- a leader pullback: 60 sessions up 0.5 a day, then 3 lighter sessions down ---------------
# The swing high is 80 at session 59; T = 62 closes 77.4 with a low of 76.9, within 1 ATR (~1.0)
# of the 10-day EMA (~77.6), on 600 shares against the 50-day average of 976.
ADVANCE = line([50 + 0.5 * i for i in range(60)])
PULLBACK = line([79.0, 78.2, 77.4], volume=600)

# --- an emerging leader: through a correction (sessions 22-29) the stock dips 5% while SPY
# falls 10.7%; at T = 34 the stock is at a new 20-session high (the tests shorten the windows).
LEADER_CLOSES = [100 + 0.5 * i for i in range(20)] + [
    109, 107, 105, 104, 104.5, 105.5, 106, 107, 108, 109,
    110, 110.5, 111, 111.5, 112,
]  # fmt: skip
SPY_CLOSES = [300.0 + i for i in range(20)] + [
    315, 308, 300, 292, 285, 288, 290, 292, 294, 295,
    296, 297, 298, 299, 300,
]  # fmt: skip

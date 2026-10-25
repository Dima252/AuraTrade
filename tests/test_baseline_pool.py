"""The 3.8 setup pool: hand-built pieces with known answers, then the pool end to end on a
synthetic history with minute bars made from its daily bars."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path

from auratrade.config import Strategy
from auratrade.data.alpaca_data import Feed
from auratrade.indicators.earnings import EarningsTiming
from auratrade.sim.minutes import Bar, MinuteCache
from auratrade.sim.trade import TradeOutcome, TradeResult
from auratrade.storage import LocalStore
from backtest import pool as bp
from backtest.baseline import Loaded, morning_notes, pool_inputs, pool_lists
from backtest.history_scan import load_history
from backtest.regime_search import Variant
from tests.ingest.fakes import RISK
from tests.strategies.scan_data import with_strategy
from tests.test_baseline_backtest import _session, found, history, series
from tests.test_scan_real_data import build_synthetic
from tests.test_smoke_real_data import SESSIONS, weekdays

DAYS = weekdays(date(2020, 1, 6), 10)


def listing(f: bp.Found, day: date) -> bp.Listing:
    return bp.Listing(f, day, 1, 0.5, 4)


def test_prices_run_in_the_entry_sessions_traded_units() -> None:
    s = series("X", DAYS, [10.0] * 10, [10.5] * 10)
    s.raw_close[5:] = s.raw_close[5:].__class__("d", [40.0] * 5)  # a 1:4 reverse split
    h = history(DAYS, {"X": s})
    assert bp.scale(h, "X", DAYS[2]) == 1.0
    assert bp.scale(h, "X", DAYS[7]) == 4.0
    assert bp.scale(h, "X", date(2020, 2, 1)) == 4.0  # after the last bar: the last one


def test_eps_enter_on_their_gap_session_the_rest_on_the_next() -> None:
    h = history(DAYS, {"X": series("X", DAYS, [10.0] * 10, [10.5] * 10)})
    p = bp.PoolInputs(h, {}, [_session(d) for d in DAYS], RISK)
    vcp = listing(found("X", 3, 10.2), DAYS[3])
    ep = listing(found("X", 3, 10.2, Strategy.EPISODIC_PIVOT), DAYS[3])
    assert bp.entry_day(p, vcp) == DAYS[4]
    assert bp.entry_day(p, ep) == DAYS[3]
    assert bp.entry_day(p, listing(found("X", 9, 10.2), DAYS[9])) is None
    assert bp.reachable(p, vcp, DAYS[4])
    assert not bp.reachable(p, listing(found("X", 3, 10.6), DAYS[3]), DAYS[4])
    assert bp.reachable(p, ep, DAYS[3])


def trade(day: date, r: float | None, status: bp.Status = bp.Status.CLOSED) -> bp.PoolTrade:
    result = None if r is None else TradeResult(TradeOutcome.CLOSED, risk=100.0, pnl=100.0 * r)
    return bp.PoolTrade(
        listing(found("X", 0, 10.0), day), day, bp.Regime.CORRECTION, status, result=result
    )


def test_expectancy_of_closed_trades_with_its_interval() -> None:
    trades = [trade(DAYS[0], 2.0), trade(DAYS[0], -1.0), trade(DAYS[1], -1.0), trade(DAYS[2], 3.0)]
    trades.append(trade(DAYS[3], 5.0, bp.Status.OPEN))  # open at the end: not counted
    e = bp.expectancy(trades)
    assert (e.trades, e.sessions, e.mean, e.win_rate) == (4, 3, 0.75, 0.5)
    assert (e.avg_gain, e.avg_loss) == (2.5, -1.0)
    assert e.interval is not None
    assert e.interval.low <= 0.75 <= e.interval.high
    assert bp.expectancy([]).interval is None
    assert not bp.expectancy([]).passes


class DailyMinutes:
    """Each symbol's minute bars made from its raw daily bar: a 0.5% dip for 3 minutes, the open
    to minute 30, a rise to the high by noon, the low at 14:00, then the close. The first 6
    minutes trade heavily, as a gap's open does (the morning EPs' 09:36 volume check)."""

    def __init__(self, bars: Mapping[tuple[str, date], tuple[float, float, float, float]]) -> None:
        self.bars = bars
        self.calls: list[tuple[date, Feed, tuple[str, ...]]] = []

    def minute_bars(
        self, symbols: Iterable[str], start: datetime, end: datetime, *, feed: Feed
    ) -> Mapping[str, Sequence[Bar]]:
        day = (start - timedelta(hours=5)).date()
        wanted = tuple(sorted(symbols))
        self.calls.append((day, feed, wanted))
        out = {}
        for symbol in wanted:
            ohlc = self.bars.get((symbol, day))
            if ohlc is None:
                continue
            o, hi, lo, c = ohlc
            path = [o * 0.995] * 3 + [o] * 27 + [o + (hi - o) * k / 120 for k in range(1, 121)]
            path += [hi + (lo - hi) * k / 120 for k in range(1, 121)]
            path += [lo + (c - lo) * k / 120 for k in range(1, 121)]
            out[symbol] = [
                Bar(start + timedelta(minutes=m), p, p, p, p, 1_000_000 if m < 6 else 10_000)
                for m, p in enumerate(path)
            ]
        return out


def test_the_pool_end_to_end_on_a_synthetic_history(tmp_path: Path) -> None:
    store, risk = build_synthetic(tmp_path)
    days = [_session(d) for d in SESSIONS]

    hist = load_history(store, days, risk, last=SESSIONS[-1])
    # Quarterly reports for every security, so the earnings blackout can be judged.
    reports = [(d, EarningsTiming.AFTER_CLOSE) for d in SESSIONS[20::63]]
    hist.earnings = dict.fromkeys(hist.series, reports)
    loaded = Loaded(hist, days, store, None)  # type: ignore[arg-type]
    p, candidates = pool_inputs(loaded, risk, Variant(2, -2, 2, 8), log=lambda _: None)
    assert candidates.found
    raw = {
        (hist.symbol(k), d): (s.open[i], s.high[i], s.low[i], s.raw_close[i])
        for k, s in hist.series.items()
        for i, d in enumerate(s.dates)
    }
    source = DailyMinutes(raw)
    cache = MinuteCache(LocalStore(tmp_path / "cache"), source)

    def symbols_for(wanted: Mapping[date, set[str]]) -> Mapping[tuple[date, str], str]:
        return bp.traded_symbols(store, wanted)

    def minutes_for(wanted: Mapping[date, set[str]]) -> Mapping[date, Mapping[str, bp.Minutes]]:
        return bp.prefetch(p, cache, wanted)

    lists, check = pool_lists(p, candidates, minutes_for, symbols_for, log=lambda _: None)
    assert lists
    assert check.passed == check.candidates == 1  # the synthetic gap opens on heavy volume
    assert morning_notes(p, lists, check)[0].startswith("**EPs at 09:36")
    trades = bp.run_pool(p, lists, minutes_for, symbols_for)
    assert trades
    statuses = Counter(t.status for t in trades)
    assert statuses[bp.Status.CLOSED] >= 1
    assert statuses[bp.Status.NOT_REACHED] >= 1
    assert {t.status for t in trades} <= set(bp.Status)
    filled = [bp.unit(t.listing) for t in trades if t.triggered]
    assert len(filled) == len(set(filled))  # a filled unit isn't entered again
    for t in trades:
        if t.status in (bp.Status.CLOSED, bp.Status.OPEN):
            assert t.result is not None
            assert t.result.risk > 0
    fetched = {d for d, _, _ in source.calls}
    gap_days = {
        hist.series[f.security].dates[f.index]
        for f in candidates.found
        if f.setup.strategy is Strategy.EPISODIC_PIVOT
    }
    assert fetched <= {t.entry_day for t in trades} | gap_days
    # A re-run reads every bar from the cache.
    calls = len(source.calls)
    again = bp.run_pool(p, lists, minutes_for, symbols_for)
    assert len(source.calls) == calls
    assert [t.status for t in again] == [t.status for t in trades]


def minute_bars(day: date, volumes: Sequence[float]) -> bp.Minutes:
    open_at = _session(day).open_at
    sip = [Bar(open_at + timedelta(minutes=m), 10, 10, 10, 10, v) for m, v in enumerate(volumes)]
    return bp.Minutes(sip, None)


def test_an_ep_reads_the_close_before_its_gap_the_rest_their_scan_session() -> None:
    assert bp.known_at(found("X", 5, 10.0, Strategy.EPISODIC_PIVOT)) == 4
    assert bp.known_at(found("X", 5, 10.0)) == 5


def test_the_opening_volume_counts_the_open_to_the_entry() -> None:
    p = bp.PoolInputs(history(DAYS, {}), {}, [_session(d) for d in DAYS], RISK)
    assert RISK.sim.entry_minutes == 6
    # Minutes 0-5 count (09:30-09:35); the 09:36 bar and later don't.
    bars = minute_bars(DAYS[0], [100] * 6 + [1_000_000] * 3)
    assert bp.opening_volume(p, DAYS[0], bars) == 600


def test_the_prior_average_volume_in_traded_shares() -> None:
    risk = with_strategy(RISK, "EPISODIC_PIVOT", volume_sessions=3)
    s = series("X", DAYS, [10.0] * 10, [10.5] * 10)
    s.volume[2:5] = s.volume.__class__("d", [3e6, 6e6, 9e6])
    s.raw_close[5:] = s.raw_close[5:].__class__("d", [20.0] * 5)  # traded 2x the adjusted price
    p = bp.PoolInputs(history(DAYS, {"X": s}), {}, [_session(d) for d in DAYS], risk)
    ep = found("X", 5, 10.0, Strategy.EPISODIC_PIVOT)
    assert bp.prior_average_volume(p, ep) == 3e6  # (3 + 6 + 9) / 3 million, halved
    assert bp.prior_average_volume(p, found("X", 2, 10.0, Strategy.EPISODIC_PIVOT)) is None


def test_morning_eps_pass_on_their_opening_volume_only() -> None:
    risk = with_strategy(RISK, "EPISODIC_PIVOT", volume_sessions=3)
    data = {k: series(k, DAYS, [10.0] * 10, [10.5] * 10) for k in ("A", "B", "C")}
    p = bp.PoolInputs(history(DAYS, data), {}, [_session(d) for d in DAYS], risk)
    loud = found("A", 5, 10.0, Strategy.EPISODIC_PIVOT)
    quiet = found("B", 5, 10.0, Strategy.EPISODIC_PIVOT)
    missing = found("C", 5, 10.0, Strategy.EPISODIC_PIVOT)
    vcp = found("C", 5, 10.0)
    day = DAYS[5]
    bars = {
        "A": minute_bars(day, [50_000] * 6),  # 300k = 30% of the 1M average
        "B": minute_bars(day, [30_000] * 6),  # 180k = 18%
    }

    def symbols_for(wanted: Mapping[date, set[str]]) -> Mapping[tuple[date, str], str]:
        assert wanted == {day: {"A", "B", "C"}}
        return {(day, k): k for k in ("A", "B", "C")}

    def minutes_for(wanted: Mapping[date, set[str]]) -> Mapping[date, Mapping[str, bp.Minutes]]:
        return {day: bars}

    check = bp.confirm_morning_eps(p, [loud, quiet, missing, vcp], minutes_for, symbols_for)
    assert check.kept == [vcp, loud]
    assert (check.candidates, check.passed, check.no_minutes) == (3, 1, 1)


def test_close_entries_are_played_without_minute_bars(tmp_path: Path) -> None:
    store, risk = build_synthetic(tmp_path)
    days = [_session(d) for d in SESSIONS]
    hist = load_history(store, days, risk, last=SESSIONS[-1])
    reports = [(d, EarningsTiming.AFTER_CLOSE) for d in SESSIONS[20::63]]
    hist.earnings = dict.fromkeys(hist.series, reports)
    loaded = Loaded(hist, days, store, None)  # type: ignore[arg-type]
    close = risk.with_candidate("CLOSE")
    p, candidates = pool_inputs(loaded, close, Variant(2, -2, 2, 8), log=lambda _: None)
    lists = bp.capped_lists(p.h, candidates.found, candidates.regimes, close)
    assert lists
    for strategy in Strategy:
        assert bp.at_close(close, strategy)
        assert not bp.at_close(risk, strategy)

    def no_minutes(wanted: Mapping[date, set[str]]) -> Mapping[date, Mapping[str, bp.Minutes]]:
        assert not any(wanted.values()), "a close entry fetched minute bars"
        return {}

    def symbols_for(wanted: Mapping[date, set[str]]) -> Mapping[tuple[date, str], str]:
        return bp.traded_symbols(store, wanted)

    trades = bp.run_pool(p, lists, no_minutes, symbols_for)
    statuses = Counter(t.status for t in trades)
    assert not statuses[bp.Status.NOT_REACHED]
    assert not statuses[bp.Status.NO_MINUTES]
    assert statuses[bp.Status.NOT_CONFIRMED] + statuses[bp.Status.CLOSED] >= 1
    filled = [bp.unit(t.listing) for t in trades if t.triggered]
    assert len(filled) == len(set(filled))
    for t in trades:
        if t.triggered:
            assert t.result is not None
            assert t.result.gate is not None
            assert t.result.gate.passed
            assert t.result.order is not None
            assert t.result.order.tranches.second == 0

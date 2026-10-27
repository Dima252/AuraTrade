"""The `simulate` job on the test database: the breakout tracker, the baseline book (decided from
T-1's list, simulated from cached minute bars) and the verdict scorecard on synthetic verdicts.

T-1 is Wed 2026-10-21 and T Thu 10-22. NVDA sits at 49 for 300 sessions, then breaks out on T
through its VCP pivot of 50: the scan data of the simulator tests (the 09:40 breakout fills
tranche 1 at 50.10 after 0.2% slippage)."""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import insert, select

from auratrade.calendar.sessions import load_calendar
from auratrade.config import Strategy
from auratrade.data.alpaca_data import Feed, MinuteBar
from auratrade.db.models import (
    BaselineSignalRow,
    BreakoutRow,
    DailyBar,
    EarningsEvent,
    VerdictOutcomeRow,
    WatchlistRow,
)
from auratrade.jobs import simulate
from auratrade.journal.evening import (
    TickerReview,
    complete_results,
    objects,
    run_baseline,
    score_verdicts,
    track_breakouts,
    tracked_for_regime,
)
from auratrade.journal.scorecard import Review, Verdict
from auratrade.sim.minutes import MinuteCache, key
from tests.execution.conftest import World

PREV, T = date(2026, 10, 21), date(2026, 10, 22)
OPEN_UTC = {
    d: datetime(d.year, d.month, d.day, 13, 30, tzinfo=UTC) for d in (T, date(2026, 10, 23))
}


class Source:
    """Minute bars by session and symbol; records each request."""

    def __init__(self, days: Mapping[date, Mapping[str, list[MinuteBar]]]) -> None:
        self.days = days
        self.calls: list[tuple[date, Feed, tuple[str, ...]]] = []

    def minute_bars(
        self, symbols: Iterable[str], start: datetime, end: datetime, *, feed: Feed
    ) -> dict[str, list[MinuteBar]]:
        day = start.date()
        wanted = tuple(sorted(symbols))
        self.calls.append((day, feed, wanted))
        return {s: list(self.days.get(day, {}).get(s, [])) for s in wanted}


def minutes(
    symbol: str, day: date, spec: Mapping[int, tuple[float, float, float, float]]
) -> list[MinuteBar]:
    return [
        MinuteBar(symbol, OPEN_UTC[day] + timedelta(minutes=m), o, h, low, c, 1000)
        for m, (o, h, low, c) in sorted(spec.items())
    ]


OPENING = dict.fromkeys(range(5), (49.5, 49.8, 49.4, 49.7)) | {5: (49.7, 49.9, 49.6, 49.85)}
BREAKOUT = OPENING | {
    10: (49.9, 50.4, 49.85, 50.3),
    200: (50.3, 50.8, 50.2, 50.6),
    369: (50.6, 50.9, 50.5, 50.8),
    380: (50.8, 51.0, 50.7, 50.9),
}
QUIET = dict.fromkeys((0, 100, 369, 385), (50.8, 51.2, 50.6, 51.0))


def daily(world: World, symbol: str, day: date, o: float, h: float, low: float, c: float) -> None:
    with world.engine.begin() as conn:
        conn.execute(
            insert(DailyBar).values(
                ticker_id=world.tickers[symbol],
                session_date=day,
                open=o,
                high=h,
                low=low,
                close=c,
                raw_close=c,
                volume=1_000_000,
            )
        )


def listed(
    world: World,
    symbol: str,
    *,
    pivot: float,
    rank: int | None,
    day: date = PREV,
    strategy: str = "VCP_BREAKOUT",
) -> None:
    with world.engine.begin() as conn:
        conn.execute(
            insert(WatchlistRow).values(
                session_date=day,
                ticker_id=world.tickers[symbol],
                symbol=symbol,
                strategy=strategy,
                pivot=pivot,
                structural_stop=pivot * 0.94,
                structure=pivot,
                score=0.8,
                rank=rank,
                regime="CONFIRMED_UPTREND",
                close=pivot * 0.98,
                atr=1.0,
                adr=0.04,
                sma_50=pivot * 0.98,
                dollar_volume=49e6,
                risk_hash=world.risk.config_hash,
            )
        )


@pytest.fixture
def market(world: World) -> World:
    """NVDA flat at 49 through T-1, AMD at 99; T-1 an uptrend; NVDA breaks out on T."""
    world.bars("NVDA", [49.0] * 300, last=PREV)
    world.bars("AMD", [99.0] * 300, last=PREV)
    daily(world, "NVDA", T, 49.5, 51.0, 49.4, 50.9)
    daily(world, "AMD", T, 99.0, 99.5, 98.5, 99.0)
    world.regime(PREV)
    world.regime(T)
    with world.engine.begin() as conn:  # a report 30 sessions back: no earnings blackout
        conn.execute(
            insert(EarningsEvent).values(
                [
                    {
                        "ticker_id": world.tickers[s],
                        "report_date": date(2026, 9, 9),
                        "timing": "BEFORE_OPEN",
                        "source": "EDGAR",
                    }
                    for s in ("NVDA", "AMD")
                ]
            )
        )
    return world


def report(world: World, symbol: str, day: date) -> None:
    with world.engine.begin() as conn:
        conn.execute(
            insert(EarningsEvent).values(
                ticker_id=world.tickers[symbol],
                report_date=day,
                timing="BEFORE_OPEN",
                source="FINNHUB",
            )
        )


def cache(world: World, source: Source) -> MinuteCache:
    return MinuteCache(world.storage, source)


def run_book(world: World, day: date, source: Source) -> dict[str, Any]:
    with world.engine.begin() as conn:
        calendar = load_calendar(conn)
        return run_baseline(conn, calendar, day, world.risk, cache(world, source))


def book_rows(world: World) -> list[BaselineSignalRow]:
    with world.engine.connect() as conn:
        return list(conn.execute(select(BaselineSignalRow).order_by(BaselineSignalRow.id)).all())


class TestTracker:
    def test_triggers_and_their_results(self, market: World) -> None:
        listed(market, "NVDA", pivot=50.0, rank=None)  # unranked: the tracker is ungated
        listed(market, "AMD", pivot=100.0, rank=1)
        with market.engine.begin() as conn:
            calendar = load_calendar(conn)
            assert track_breakouts(conn, calendar, T, market.risk) == {"triggers": 1, "results": 0}
            assert track_breakouts(conn, calendar, T, market.risk)["triggers"] == 1  # idempotent
        later = [date(2026, 10, d) for d in (23, 26, 27, 28, 29)]
        for i, day in enumerate(later):
            daily(market, "NVDA", day, 51, 52, 50.5, 51.0 + i * 0.1)
        with market.engine.begin() as conn:
            calendar = load_calendar(conn)
            assert complete_results(conn, calendar, later[-2], market.risk) == 0
            assert complete_results(conn, calendar, later[-1], market.risk) == 1
            [row] = objects(conn, select(BreakoutRow))
            tracked = tracked_for_regime(conn, calendar, later[-1], market.risk)
        assert (row.symbol, row.trigger_session, row.result_session) == ("NVDA", T, later[-1])
        assert row.above_trigger is True
        assert [(b.sessions_ago, b.above_trigger) for b in tracked] == [(5, True)]

    def test_a_result_completes_without_bars(self, market: World) -> None:
        with market.engine.begin() as conn:
            conn.execute(
                insert(BreakoutRow).values(
                    ticker_id=market.tickers["MSFT"],  # no bars at all
                    symbol="MSFT",
                    strategy="VCP_BREAKOUT",
                    watchlist_session=date(2026, 9, 1),
                    trigger_session=date(2026, 9, 2),
                    pivot=200.0,
                    trigger_close=201.0,
                )
            )
            assert complete_results(conn, load_calendar(conn), T, market.risk) == 1
            [row] = objects(conn, select(BreakoutRow))
        assert (row.result_close, row.above_trigger) == (201.0, True)


class TestBaseline:
    def test_takes_the_list_in_rank_order_and_simulates_the_entry(self, market: World) -> None:
        listed(market, "NVDA", pivot=50.0, rank=1)
        source = Source({T: {"NVDA": minutes("NVDA", T, BREAKOUT)}})
        out = run_book(market, T, source)
        assert out["approved"] == 1
        assert out["multiplier"] == market.risk.multiplier.prove_it_multiplier
        [row] = book_rows(market)
        assert (row.symbol, row.status, row.rank, row.conviction) == ("NVDA", "OPEN", 1, 4)
        # $5,000 x 1% x 0.5 (prove it first) = $25 over 51 - 47 = 6 shares: 3 now, 3 on the gate.
        assert row.risk_amount == pytest.approx(25.0)
        assert row.result["fills"][0] | {} == {
            "session": 1,
            "kind": "ENTRY",
            "shares": 3,
            "price": 50.10,
            "rule": None,
        }
        assert row.sessions_simulated == 1
        # Fetched once per feed for the symbol, then served from the cache.
        assert {(d, f) for d, f, _ in source.calls} == {(T, Feed.SIP), (T, Feed.IEX)}
        assert market.storage.exists(key(T, Feed.SIP))

    def test_the_next_evening_replays_the_open_trade_and_holds_it(self, market: World) -> None:
        listed(market, "NVDA", pivot=50.0, rank=1)
        nxt = date(2026, 10, 23)
        source = Source(
            {T: {"NVDA": minutes("NVDA", T, BREAKOUT)}, nxt: {"NVDA": minutes("NVDA", nxt, QUIET)}}
        )
        run_book(market, T, source)
        daily(market, "NVDA", nxt, 50.8, 51.2, 50.6, 51.0)
        out = run_book(market, nxt, source)
        assert out["listed"] == 0
        assert out["simulated"] == {"OPEN": 1}
        [row] = book_rows(market)
        assert row.sessions_simulated == 2
        assert run_book(market, nxt, source) == out  # a re-run writes the same

    def test_a_rejection_is_recorded(self, market: World) -> None:
        listed(market, "NVDA", pivot=50.0, rank=1)
        with market.engine.begin() as conn:
            conn.execute(
                WatchlistRow.__table__.update().values(structural_stop=40.0)  # 22% wide
            )
        out = run_book(market, T, Source({}))
        [row] = book_rows(market)
        assert (out["approved"], row.status, row.findings[-1]["rule"]) == (
            0,
            "REJECTED",
            "STOP_WIDTH",
        )

    def test_eps_wait_for_the_morning_candidate_list(self, market: World) -> None:
        listed(market, "NVDA", pivot=50.0, rank=1, strategy="EPISODIC_PIVOT")
        assert run_book(market, T, Source({}))["listed"] == 0
        assert book_rows(market) == []

    def test_a_report_after_t_counts_the_sessions_ahead(self, market: World) -> None:
        # 9 sessions after T: no blackout, and no cushion until the session before it.
        report(market, "NVDA", date(2026, 11, 4))
        listed(market, "NVDA", pivot=50.0, rank=1)
        run_book(market, T, Source({T: {"NVDA": minutes("NVDA", T, BREAKOUT)}}))
        [row] = book_rows(market)
        assert (row.status, row.exit_rule) == ("OPEN", None)
        report(market, "AMD", date(2026, 10, 27))  # 3 sessions after T: blacked out
        listed(market, "AMD", pivot=100.0, rank=2)
        run_book(market, T, Source({T: {"NVDA": minutes("NVDA", T, BREAKOUT)}}))
        rows = {r.symbol: r for r in book_rows(market)}
        assert rows["AMD"].findings[-1]["rule"] == "EARNINGS_BLACKOUT"

    def test_uncorrelated_setups_are_not_a_cluster(self, market: World) -> None:
        for i, symbol in enumerate(("AAPL", "META", "MSFT")):
            rng = random.Random(i)  # noqa: S311 - test data
            closes = [50.0]
            for _ in range(299):
                closes.append(closes[-1] * (1 + rng.gauss(0, 0.02)))
            market.bars(symbol, closes, last=PREV)
            report(market, symbol, date(2026, 9, 9))
            listed(market, symbol, pivot=closes[-1] * 1.02, rank=i + 1)
        out = run_book(market, T, Source({}))
        assert out["approved"] == 3

    def test_no_regime_no_book(self, world: World) -> None:
        assert run_book(world, T, Source({})) == {"skipped": f"no computed regime for {PREV}"}


class TestScorecard:
    def test_five_reviews_of_one_setup_are_one_unit(self, market: World) -> None:
        listed(market, "NVDA", pivot=50.0, rank=1)
        reviews = [
            TickerReview(
                market.tickers["NVDA"],
                Review(
                    "NVDA", Strategy.VCP_BREAKOUT, 50.0, day, verdict, 4, "v1", "claude-opus-5-5"
                ),
            )
            for day, verdict in [
                (date(2026, 10, 15), Verdict.SKIP),
                (date(2026, 10, 16), Verdict.SKIP),
                (date(2026, 10, 19), Verdict.WATCH),
                (date(2026, 10, 20), Verdict.BUY),
                (PREV, Verdict.BUY),
            ]
        ]
        source = Source({T: {"NVDA": minutes("NVDA", T, BREAKOUT)}})
        with market.engine.begin() as conn:
            out = score_verdicts(
                conn, load_calendar(conn), T, market.risk, cache(market, source), reviews
            )
            rows = objects(conn, select(VerdictOutcomeRow))
        assert out == {"triggered": 1, "units": 1}
        [row] = rows
        assert (row.verdict, row.verdict_session, row.reviews) == ("BUY", PREV, 5)
        assert (row.triggered, row.trigger_session, row.outcome) == (True, T, "OPEN")
        assert row.regime_at_trigger == "CONFIRMED_UPTREND"

    def test_a_miss_is_an_attempt_and_a_trigger_is_followed_forward(self, market: World) -> None:
        nxt = date(2026, 10, 23)
        listed(market, "NVDA", pivot=52.0, rank=1)  # T's high of 51 doesn't reach it
        listed(market, "NVDA", pivot=52.0, rank=1, day=T)
        daily(market, "NVDA", nxt, 51.5, 53.0, 51.4, 52.8)
        market.regime(nxt)
        review = Review("NVDA", Strategy.VCP_BREAKOUT, 52.0, PREV, Verdict.SKIP, None, "v1", "m")
        reviews = [TickerReview(market.tickers["NVDA"], review)]
        breakout = {m: (o + 2, h + 2, lo + 2, c + 2) for m, (o, h, lo, c) in BREAKOUT.items()}
        source = Source(
            {
                T: {"NVDA": minutes("NVDA", T, BREAKOUT)},
                nxt: {"NVDA": minutes("NVDA", nxt, breakout)},
            }
        )

        def score(day: date) -> dict[str, int]:
            with market.engine.begin() as conn:
                return score_verdicts(
                    conn, load_calendar(conn), day, market.risk, cache(market, source), reviews
                )

        def unit() -> VerdictOutcomeRow:
            with market.engine.connect() as conn:
                [row] = objects(conn, select(VerdictOutcomeRow))
            return row

        assert score(T) == {"attempts": 1, "units": 1}
        assert (unit().triggered, unit().outcome, unit().attempts) == (False, "MISSED", 1)
        assert score(nxt) == {"triggered": 1, "units": 1}
        assert (unit().triggered, unit().trigger_session, unit().verdict) == (True, nxt, "SKIP")
        listed(market, "NVDA", pivot=52.0, rank=1, day=nxt)  # still listed: no new attempt
        daily(market, "NVDA", date(2026, 10, 26), 52.8, 53.5, 52.5, 53.2)
        market.regime(date(2026, 10, 26))
        assert score(date(2026, 10, 26)) == {"followed": 1, "units": 1}
        assert len(unit().details["result"]["marks"]) == 2

    def test_no_verdicts_nothing_to_score(self, market: World) -> None:
        with market.engine.begin() as conn:
            out = score_verdicts(
                conn, load_calendar(conn), T, market.risk, cache(market, Source({})), []
            )
        assert out == {"units": 0}


class NoFunds:
    def daily_bars(self, *_args: object, **_kwargs: object) -> dict[str, list[object]]:
        raise AssertionError("no bars are fetched before the trial starts")


def test_the_job_runs_every_step(market: World) -> None:
    listed(market, "NVDA", pivot=50.0, rank=1)
    source = Source({T: {"NVDA": minutes("NVDA", T, BREAKOUT)}})
    spec = simulate.make_job(
        engine_factory=lambda _s: market.engine,
        store_factory=lambda _s: market.storage,
        source_factory=lambda _s: source,
        funds_factory=lambda _s: NoFunds(),
    )
    out = dict(spec.run(market.context("simulate", session=T)) or {})
    assert out["books"] == {"skipped": "the trial hasn't started"}
    assert out["tracker"] == {"triggers": 1, "results": 0}
    assert out["baseline"]["simulated"] == {"OPEN": 1}  # type: ignore[index]
    assert out["scorecard"] == {"units": 0}


def test_the_job_skips_incomplete_data(market: World) -> None:
    market.set_session(T, data_status="INCOMPLETE")
    spec = simulate.make_job(engine_factory=lambda _s: market.engine)
    assert spec.run(market.context("simulate", session=T)) == {"skipped": "data status INCOMPLETE"}

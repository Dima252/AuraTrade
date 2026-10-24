"""The minute-bar cache (sim.minutes)."""

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from auratrade.data.alpaca_data import Feed, MinuteBar
from auratrade.sim.minutes import MinuteCache, key
from auratrade.storage import LocalStore

DAY = date(2026, 10, 22)
OPEN = datetime(2026, 10, 22, 13, 30, tzinfo=UTC)
CLOSE = OPEN + timedelta(hours=6, minutes=30)


class Source:
    def __init__(self) -> None:
        self.calls: list[tuple[Feed, tuple[str, ...]]] = []

    def minute_bars(
        self, symbols: Iterable[str], start: datetime, end: datetime, *, feed: Feed
    ) -> dict[str, list[MinuteBar]]:
        wanted = tuple(symbols)
        self.calls.append((feed, wanted))
        # QUIET never trades on IEX.
        return {
            s: [MinuteBar(s, start, 10.0, 10.5, 9.5, 10.2, 300)]
            for s in wanted
            if not (s == "QUIET" and feed is Feed.IEX)
        }


def test_fetches_once_and_merges_new_symbols(tmp_path: Path) -> None:
    source = Source()
    cache = MinuteCache(LocalStore(tmp_path), source)
    first = cache.minutes(DAY, ["AAA", "QUIET"], open_at=OPEN, close_at=CLOSE)
    assert first["AAA"].sip[0].close == 10.2
    assert first["QUIET"].iex == []
    again = cache.minutes(DAY, ["AAA", "QUIET", "BBB"], open_at=OPEN, close_at=CLOSE)
    assert again["AAA"] == first["AAA"]
    assert source.calls == [
        (Feed.SIP, ("AAA", "QUIET")),
        (Feed.IEX, ("AAA", "QUIET")),
        (Feed.SIP, ("BBB",)),
        (Feed.IEX, ("BBB",)),
    ]
    assert LocalStore(tmp_path).exists(key(DAY, Feed.IEX))


def test_no_sip_bar_no_minutes_and_no_iex_before_its_history(tmp_path: Path) -> None:
    class Empty(Source):
        def minute_bars(
            self, symbols: Iterable[str], start: datetime, end: datetime, *, feed: Feed
        ) -> dict[str, list[MinuteBar]]:
            return {}

    assert (
        MinuteCache(LocalStore(tmp_path), Empty()).minutes(
            DAY, ["AAA"], open_at=OPEN, close_at=CLOSE
        )
        == {}
    )
    old = date(2019, 5, 1)
    source = Source()
    got = MinuteCache(LocalStore(tmp_path), source).minutes(
        old, ["AAA"], open_at=OPEN, close_at=CLOSE
    )
    assert got["AAA"].iex is None
    assert [f for f, _ in source.calls] == [Feed.SIP]

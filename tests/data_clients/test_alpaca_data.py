from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest

from auratrade.data.alpaca_data import (
    Adjustment,
    AlpacaDataClient,
    CorporateActionKind,
    Feed,
    alpaca_http,
)
from auratrade.data.http import VendorError
from tests.data_clients.fake import Server, fixture_json, ok

KEY, SECRET = "PKTESTKEY", "test-secret-value"
# Friday 2026-10-09, 17:00 ET: the evening ingest.
EVENING = datetime(2026, 10, 9, 21, 0, tzinfo=UTC)


def client(server: Server, now: datetime = EVENING) -> AlpacaDataClient:
    http = alpaca_http(KEY, SECRET, client=server.client(), sleep=lambda _: None)
    return AlpacaDataClient(http, now=lambda: now)


def daily_pages() -> Server:
    return Server(*(ok(fixture_json(f"alpaca/bars_daily_page{i}.json")) for i in (1, 2, 3)))


def test_daily_bars_follow_pages_and_date_bars_by_session() -> None:
    server = daily_pages()
    bars = client(server).daily_bars(["DXJ", "AAPL"], date(2026, 10, 6), date(2026, 10, 9))
    assert [b.session for b in bars["AAPL"]] == [date(2026, 10, d) for d in (6, 7, 8, 9)]
    assert [b.session for b in bars["DXJ"]] == [date(2026, 10, d) for d in (6, 7, 8, 9)]
    first = bars["AAPL"][0]
    assert (first.open, first.high, first.low, first.close) == (332.28, 334.38, 330.62, 333.63)
    assert first.volume == 30604659
    # DXJ split 3:1 on 10-09; `adjustment=split` already shows the post-split price.
    assert bars["DXJ"][-1].close == pytest.approx(60.71)
    tokens = [server.params(i).get("page_token") for i in range(3)]
    assert tokens[0] is None
    assert tokens[1]
    assert tokens[2]


def test_daily_request_parameters_and_keys() -> None:
    server = daily_pages()
    client(server).daily_bars(["AAPL", "DXJ", "AAPL"], date(2026, 10, 6), date(2026, 10, 8))
    request = server.requests[0]
    assert request.url.host == "data.alpaca.markets"
    assert request.url.path == "/v2/stocks/bars"
    assert request.headers["APCA-API-KEY-ID"] == KEY
    assert request.headers["APCA-API-SECRET-KEY"] == SECRET
    params = server.params(0)
    assert params["symbols"] == "AAPL,DXJ"
    assert (params["timeframe"], params["feed"], params["adjustment"]) == ("1Day", "sip", "split")
    assert params["start"] == "2026-10-06T04:00:00Z"
    # A past day's request ends after its extended session (20:00 ET).
    assert params["end"] == "2026-10-09T00:00:00Z"


def test_daily_end_today_is_clipped_to_the_sip_delay() -> None:
    server = daily_pages()
    client(server).daily_bars(["AAPL"], date(2026, 10, 6), date(2026, 10, 9))
    # 17:00 ET minus 16 minutes: recent SIP would be refused.
    assert server.params(0)["end"] == "2026-10-09T20:44:00Z"


def test_daily_raw_adjustment() -> None:
    server = daily_pages()
    client(server).daily_bars(
        ["AAPL"], date(2026, 10, 6), date(2026, 10, 8), adjustment=Adjustment.RAW
    )
    assert server.params(0)["adjustment"] == "raw"


def test_nothing_is_requested_when_the_window_is_still_inside_the_delay() -> None:
    server = Server()
    morning = datetime(2026, 10, 9, 4, 10, tzinfo=UTC)  # 00:10 ET
    assert client(server, morning).daily_bars(["AAPL"], date(2026, 10, 9), date(2026, 10, 9)) == {}
    assert server.requests == []


def test_more_than_a_thousand_symbols_go_in_chunks() -> None:
    symbols = [f"S{i:04d}" for i in range(1500)]
    server = Server(ok({"bars": {}, "next_page_token": None}), ok({"bars": None}))
    client(server).daily_bars(symbols, date(2026, 10, 6), date(2026, 10, 8))
    assert [len(server.params(i)["symbols"].split(",")) for i in range(2)] == [1000, 500]


def test_recent_sip_refusal_is_a_clear_error() -> None:
    recorded = fixture_json("alpaca/bars_recent_sip_error.json")
    server = Server(httpx.Response(recorded["status"], json=recorded["body"]))
    with pytest.raises(VendorError, match="recent SIP data") as info:
        client(server).daily_bars(["SPY"], date(2026, 10, 6), date(2026, 10, 8))
    assert (info.value.status, info.value.retryable) == (403, False)
    assert SECRET not in str(info.value)


def test_iex_minute_bars_are_not_clipped() -> None:
    server = Server(ok(fixture_json("alpaca/bars_minute_iex.json")))
    start = datetime(2026, 10, 9, 13, 30, tzinfo=UTC)
    end = datetime(2026, 10, 9, 13, 34, 59, tzinfo=UTC)
    bars = client(server, now=start).minute_bars(["SPY"], start, end, feed=Feed.IEX)["SPY"]
    assert [b.start.minute for b in bars] == [30, 31, 32, 33, 34]
    assert bars[0].start.tzinfo is UTC
    assert (bars[0].open, bars[0].high, bars[0].volume) == (776.23, 776.59, 7148)
    params = server.params(0)
    assert (params["feed"], params["timeframe"], params["adjustment"]) == ("iex", "1Min", "raw")
    assert params["end"] == "2026-10-09T13:34:59Z"


def test_sip_minute_bars_are_clipped() -> None:
    server = Server(ok({"bars": {}, "next_page_token": None}))
    now = datetime(2026, 10, 9, 13, 40, tzinfo=UTC)
    start = datetime(2026, 10, 9, 13, 30, tzinfo=UTC)
    assert client(server, now).minute_bars(["SPY"], start, now, feed=Feed.SIP) == {}
    server = Server()
    early = datetime(2026, 10, 9, 13, 35, tzinfo=UTC)
    assert client(server, early).minute_bars(["SPY"], start, early, feed=Feed.SIP) == {}
    assert server.requests == []


def test_naive_datetimes_are_refused() -> None:
    naive = datetime(2026, 10, 9, 13, 30)  # noqa: DTZ001 - the case under test
    with pytest.raises(ValueError, match="timezone-aware"):
        client(Server()).minute_bars(["SPY"], naive, naive, feed=Feed.IEX)


def test_latest_trades_and_quotes() -> None:
    server = Server(
        ok(fixture_json("alpaca/latest_trades_iex.json")),
        ok(fixture_json("alpaca/latest_quotes_iex.json")),
    )
    alpaca = client(server)
    trades = alpaca.latest_trades(["SPY", "AAPL"])
    quotes = alpaca.latest_quotes(["SPY", "AAPL"])
    assert trades["SPY"].price == 769.56
    assert trades["SPY"].size == 100
    assert trades["SPY"].at == datetime(2026, 10, 15, 16, 56, 32, 503764, tzinfo=UTC)
    aapl = quotes["AAPL"]
    assert (aapl.bid, aapl.ask, aapl.bid_size, aapl.ask_size) == (343.85, 343.88, 80, 40)
    assert [server.params(i)["feed"] for i in range(2)] == ["iex", "iex"]
    assert [r.url.path for r in server.requests] == [
        "/v2/stocks/trades/latest",
        "/v2/stocks/quotes/latest",
    ]


def test_corporate_actions_split_and_dividends() -> None:
    server = Server(ok(fixture_json("alpaca/corporate_actions.json")))
    actions = client(server).corporate_actions(
        date(2026, 10, 1),
        date(2026, 10, 15),
        symbols=["QDTE", "DXJ"],
        kinds=[CorporateActionKind.SPLIT, CorporateActionKind.CASH_DIVIDEND],
    )
    (split,) = [a for a in actions if a.kind is CorporateActionKind.SPLIT]
    assert (split.symbol, split.effective_date, split.ratio) == ("DXJ", date(2026, 10, 9), 3.0)
    assert split.source_id == "9d1bd707-e3ac-4baa-bd79-492fdf593e77"
    assert "id" not in split.terms
    dividends = [a for a in actions if a.kind is CorporateActionKind.CASH_DIVIDEND]
    assert [(d.effective_date, d.process_date) for d in dividends] == [
        (date(2026, 10, 1), date(2026, 10, 2)),
        (date(2026, 10, 8), date(2026, 10, 9)),
    ]
    assert dividends[0].ratio is None
    assert dividends[0].terms["rate"] == 0.110957
    params = server.params(0)
    assert params["types"] == "cash_dividend,forward_split"
    assert params["symbols"] == "DXJ,QDTE"
    assert (params["start"], params["end"]) == ("2026-10-01", "2026-10-15")


def test_corporate_actions_mergers_spin_offs_and_renames_across_pages() -> None:
    page1 = fixture_json("alpaca/corporate_actions_other_page1.json")
    page2 = fixture_json("alpaca/corporate_actions_spinoff_rename.json")
    page2["corporate_actions"]["unit_splits"] = [{"id": "ignored"}]
    server = Server(ok(page1), ok(page2))
    actions = client(server).corporate_actions(date(2026, 6, 1), date(2026, 10, 15))
    kinds = {a.kind for a in actions}
    assert kinds == {
        CorporateActionKind.CASH_MERGER,
        CorporateActionKind.STOCK_MERGER,
        CorporateActionKind.SPIN_OFF,
        CorporateActionKind.SYMBOL_CHANGE,
    }
    stock = next(a for a in actions if a.kind is CorporateActionKind.STOCK_MERGER)
    assert (stock.symbol, stock.effective_date) == ("470CNT015", date(2026, 9, 8))
    assert stock.terms["acquirer_symbol"] == "JAGX"
    spin = next(a for a in actions if a.kind is CorporateActionKind.SPIN_OFF)
    assert (spin.symbol, spin.effective_date) == ("ABVC", date(2026, 8, 24))
    rename = next(a for a in actions if a.symbol == "AAUM")
    assert rename.effective_date == date(2026, 7, 6)  # a name change has only a process date
    assert rename.terms["new_symbol"] == "PRVT"
    assert "symbols" not in server.params(0)
    assert server.params(1)["page_token"] == page1["next_page_token"]
    assert server.params(0)["types"] == (
        "cash_dividend,cash_merger,forward_split,name_change,reverse_split,spin_off,stock_merger"
    )


def test_assets_come_from_the_paper_trading_api() -> None:
    server = Server(ok(fixture_json("alpaca/assets.json")))
    assets = {a.symbol: a for a in client(server).assets()}
    assert {"SPY", "AAPL", "BRK.B"} <= set(assets)
    assert assets["AAPL"].exchange == "NASDAQ"
    assert assets["AAPL"].tradable
    assert "has_options" in assets["SPY"].attributes
    request = server.requests[0]
    assert request.url.host == "paper-api.alpaca.markets"
    assert server.params(0) == {"status": "active", "asset_class": "us_equity"}


def test_unexpected_shapes_are_errors() -> None:
    with pytest.raises(VendorError, match="unexpected response"):
        client(Server(ok({"assets": []}))).assets()
    with pytest.raises(VendorError, match="unexpected response"):
        client(Server(ok([]))).daily_bars(["AAPL"], date(2026, 10, 6), date(2026, 10, 8))

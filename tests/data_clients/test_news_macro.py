from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest

from auratrade.data.alpaca_data import alpaca_http
from auratrade.data.http import VendorError
from auratrade.data.macro import CboeClient, CboeIndex, FredClient, FredSeries
from auratrade.data.news import AlpacaNewsClient
from tests.data_clients.fake import Server, fixture_json, fixture_text, ok

FRED_KEY = "abcdef0123456789abcdef0123456789"


def news_client(server: Server) -> AlpacaNewsClient:
    return AlpacaNewsClient(alpaca_http("K", "S", client=server.client(), sleep=lambda _: None))


def news_pages() -> Server:
    last = fixture_json("alpaca/news_page2.json")
    last["next_page_token"] = None
    return Server(ok(fixture_json("alpaca/news_page1.json")), ok(last))


START = datetime(2026, 10, 8, tzinfo=UTC)
END = datetime(2026, 10, 9, 23, 59, 59, tzinfo=UTC)


def test_news_follows_pages_oldest_first() -> None:
    server = news_pages()
    items = news_client(server).news(["AAPL"], START, END)
    assert [n.id for n in items] == [62237744, 62238712, 62239061, 62241998]
    assert items[0].created_at == datetime(2026, 10, 8, 7, 31, 32, tzinfo=UTC)
    assert items[1].symbols[:3] == ("AAPL", "ALLE", "ALRM")
    assert (
        server.params(1)["page_token"] == fixture_json("alpaca/news_page1.json")["next_page_token"]
    )
    params = server.params(0)
    assert (params["symbols"], params["sort"], params["limit"]) == ("AAPL", "asc", "50")
    assert (params["start"], params["end"]) == ("2026-10-08T00:00:00Z", "2026-10-09T23:59:59Z")
    assert params["include_content"] == "false"


def test_news_text_is_unescaped_and_content_only_on_request() -> None:
    items = news_client(news_pages()).news(["AAPL"], START, END)
    assert "&#39;" not in items[0].summary
    assert "Apple's" in items[0].summary
    assert all(n.content is None for n in items)


def test_news_dedupes_items_across_symbol_chunks() -> None:
    page = fixture_json("alpaca/news_page1.json")
    page["next_page_token"] = None
    server = Server(ok(page), ok(page))
    symbols = [f"S{i:03d}" for i in range(150)]
    items = news_client(server).news(symbols, START, END, include_content=True)
    assert len(server.requests) == 2
    assert [n.id for n in items] == [62237744, 62238712]
    assert server.params(0)["include_content"] == "true"


def test_news_for_every_symbol_sends_no_symbol_filter() -> None:
    server = news_pages()
    items = news_client(server).news(None, START, END)
    assert len(items) == 4
    assert "symbols" not in server.params(0)


def test_news_unexpected_shape() -> None:
    with pytest.raises(VendorError, match="unexpected response"):
        news_client(Server(ok([]))).news(["AAPL"], START, END)


def cboe(server: Server) -> CboeClient:
    return CboeClient(client=server.client(), sleep=lambda _: None)


def test_cboe_delayed_quote_reads_new_york_wall_clock() -> None:
    server = Server(ok(fixture_json("cboe/quote_VIX.json")))
    quote = cboe(server).delayed_quote(CboeIndex.VIX)
    assert (quote.current_price, quote.close, quote.prev_day_close) == (16.76, 16.76, 16.87)
    # "2026-10-15T13:28:31" is New York time (EDT), 15 minutes behind the 17:43 UTC stamp.
    assert quote.last_trade_at == datetime(2026, 10, 15, 17, 28, 31, tzinfo=UTC)
    assert server.requests[0].url.path == "/api/global/delayed_quotes/quotes/_VIX.json"


def test_cboe_quote_without_data_is_an_error() -> None:
    with pytest.raises(VendorError, match="no data"):
        cboe(Server(ok({"data": None}))).delayed_quote(CboeIndex.VIX3M)


def test_cboe_history_parses_us_dates() -> None:
    server = Server(ok(fixture_text("cboe/VIX3M_History.csv")))
    history = cboe(server).history(CboeIndex.VIX3M)
    assert history.closes[date(2026, 10, 9)] == 17.77
    assert history.closes[date(2009, 9, 18)] == 26.54
    assert history.last_date == date(2026, 10, 14)
    assert server.requests[0].url.path == "/api/global/us_indices/daily_prices/VIX3M_History.csv"


def test_cboe_empty_history() -> None:
    history = cboe(Server(ok("DATE,OPEN,HIGH,LOW,CLOSE\n"))).history(CboeIndex.VIX)
    assert history.closes == {}
    assert history.last_date is None


def fred(server: Server) -> FredClient:
    return FredClient(FRED_KEY, client=server.client(), sleep=lambda _: None)


def test_fred_observations_skip_missing_values() -> None:
    body = fixture_json("fred/observations_VIXCLS.json")
    body["observations"].append({"date": "2026-10-15", "value": "."})
    server = Server(ok(body))
    values = fred(server).observations(FredSeries.VIX, date(2026, 9, 28), date(2026, 10, 15))
    assert values[date(2026, 10, 9)] == 14.84
    assert date(2026, 10, 15) not in values
    assert len(values) == 13
    params = server.params(0)
    assert (params["series_id"], params["api_key"], params["file_type"]) == (
        "VIXCLS",
        FRED_KEY,
        "json",
    )
    assert (params["observation_start"], params["observation_end"]) == ("2026-09-28", "2026-10-15")


def test_fred_errors_never_show_the_key() -> None:
    recorded = fixture_json("fred/error_bad_key.json")
    server = Server(httpx.Response(recorded["status"], json=recorded["body"]))
    with pytest.raises(VendorError, match="HTTP 400") as info:
        fred(server).observations(FredSeries.VIX3M, date(2026, 10, 1), date(2026, 10, 14))
    assert FRED_KEY not in str(info.value)
    assert "api_key=" not in str(info.value)


def test_fred_unexpected_shape() -> None:
    with pytest.raises(VendorError, match="unexpected response"):
        fred(Server(ok([]))).observations(
            FredSeries.CREDIT_SPREAD, date(2026, 1, 1), date(2026, 2, 1)
        )

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest

from auratrade.data.edgar import EdgarClient
from auratrade.data.finnhub import FinnhubClient
from auratrade.data.http import VendorError
from auratrade.data.nasdaq import NasdaqDirectoryClient, parse_directory
from tests.data_clients.fake import Server, fixture_json, fixture_text, ok

AGENT = "AuraTrade research test@example.com"
FINNHUB_KEY = "finnhub-test-key-123"
APPLE = 320193


def edgar(server: Server) -> EdgarClient:
    return EdgarClient(AGENT, client=server.client(), sleep=lambda _: None)


def test_edgar_needs_a_contact_email() -> None:
    with pytest.raises(ValueError, match="contact email"):
        EdgarClient("AuraTrade")


def test_company_tickers() -> None:
    server = Server(ok(fixture_json("edgar/company_tickers.json")))
    tickers = {t.ticker: t for t in edgar(server).company_tickers()}
    assert tickers["AAPL"].cik == APPLE
    assert "BRK-B" in tickers
    assert server.requests[0].headers["User-Agent"] == AGENT


def test_company_tickers_unexpected_shape() -> None:
    with pytest.raises(VendorError, match="unexpected response"):
        edgar(Server(ok([]))).company_tickers()


def submissions_server() -> Server:
    return Server(
        ok(fixture_json("edgar/submissions_CIK0000320193.json")),
        ok(fixture_json("edgar/CIK0000320193-submissions-001.json")),
    )


def test_submissions_merge_recent_and_older_pages() -> None:
    server = submissions_server()
    subs = edgar(server).submissions(APPLE)
    assert subs is not None
    assert (subs.name, subs.sic, subs.sic_description) == (
        "Apple Inc.",
        "3571",
        "Electronic Computers",
    )
    assert "AAPL" in subs.tickers
    assert [r.url.path for r in server.requests] == [
        "/submissions/CIK0000320193.json",
        "/submissions/CIK0000320193-submissions-001.json",
    ]
    accepted = [f.accepted_at for f in subs.filings]
    assert accepted == sorted(accepted, reverse=True)
    assert subs.filings[-1].accepted_at.year == 2015  # from the older page


def test_earnings_releases_are_8k_item_202_in_utc() -> None:
    subs = edgar(submissions_server()).submissions(APPLE)
    assert subs is not None
    releases = subs.earnings_releases()
    assert releases
    assert all(f.form == "8-K" and "2.02" in f.items for f in releases)
    latest = releases[0]
    # Apple reports after the close: 00:30 UTC is 20:30 ET on the filing date's eve.
    assert latest.accepted_at == datetime(2026, 7, 31, 0, 30, 28, tzinfo=UTC)
    assert latest.items == ("2.02", "9.01")
    assert latest.filed == date(2026, 7, 30)
    assert not any(f.is_earnings_release for f in subs.filings if f.form == "10-Q")


def test_older_pages_are_skipped_before_since() -> None:
    server = Server(ok(fixture_json("edgar/submissions_CIK0000320193.json")))
    subs = edgar(server).submissions(APPLE, since=date(2016, 1, 1))
    assert subs is not None
    assert len(server.requests) == 1
    assert min(f.filed for f in subs.filings).year >= 2016


def test_unknown_cik_is_none() -> None:
    server = Server(httpx.Response(404), httpx.Response(404))
    assert edgar(server).submissions(1) is None
    assert edgar(server).company_facts(1) is None


def test_company_facts_values_keep_the_filed_date() -> None:
    server = Server(ok(fixture_json("edgar/companyfacts_CIK0000320193.json")))
    facts = edgar(server).company_facts(APPLE)
    assert facts is not None
    assert (facts.cik, facts.name) == (APPLE, "Apple Inc.")
    revenue = facts.values("RevenueFromContractWithCustomerExcludingAssessedTax", "USD")
    quarter = revenue[-1]
    assert (quarter.start, quarter.end) == (date(2026, 3, 29), date(2026, 6, 27))
    assert quarter.value == 109_417_000_000
    assert (quarter.fiscal_year, quarter.fiscal_period, quarter.form) == (2026, "Q3", "10-Q")
    assert quarter.filed == date(2026, 7, 31)
    assert quarter.frame == "CY2026Q2"
    eps = facts.values("EarningsPerShareDiluted", "USD/shares")
    assert eps[-1].value == 2.02
    assert facts.values("EarningsPerShareDiluted", "USD") == ()
    assert facts.values("NoSuchTag", "USD") == ()
    assert server.requests[0].url.path == "/api/xbrl/companyfacts/CIK0000320193.json"


def finnhub(server: Server) -> FinnhubClient:
    return FinnhubClient(FINNHUB_KEY, client=server.client(), sleep=lambda _: None)


def test_finnhub_calendar_maps_timing_and_sends_the_key_as_a_header() -> None:
    server = Server(ok(fixture_json("finnhub/calendar_earnings.json")))
    rows = {r.symbol: r for r in finnhub(server).earnings_calendar(date(2026, 10, 20))}
    assert rows["ADC"].timing == "AFTER_CLOSE"
    assert rows["ALLY"].timing == "BEFORE_OPEN"
    assert rows["ADX"].timing == "UNKNOWN"
    assert rows["ADC"].revenue_estimate == 211_033_844
    assert rows["ADC"].eps_estimate == 0.4848
    assert rows["ADC"].eps_actual is None
    assert (rows["ADC"].fiscal_year, rows["ADC"].fiscal_quarter) == (2026, 3)
    request = server.requests[0]
    assert request.headers["X-Finnhub-Token"] == FINNHUB_KEY
    assert FINNHUB_KEY not in str(request.url)
    assert server.params(0) == {"from": "2026-10-20", "to": "2026-10-20"}


def test_finnhub_calendar_during_market_timing() -> None:
    body = {"earningsCalendar": [{"symbol": "X", "date": "2026-10-20", "hour": "dmh"}]}
    (row,) = finnhub(Server(ok(body))).earnings_calendar(date(2026, 10, 20))
    assert row.timing == "DURING_MARKET"
    assert row.revenue_actual is None


def test_finnhub_full_calendar_page_is_an_error() -> None:
    row = {"symbol": "X", "date": "2026-11-02", "hour": ""}
    server = Server(ok({"earningsCalendar": [row] * 1500}))
    with pytest.raises(VendorError, match="1500-row cap"):
        finnhub(server).earnings_calendar(date(2026, 11, 2))


def test_finnhub_surprises() -> None:
    server = Server(ok(fixture_json("finnhub/stock_earnings.json")))
    latest = finnhub(server).earnings_surprises("AAPL")[0]
    assert latest.period == date(2026, 6, 30)
    assert (latest.actual, latest.estimate, latest.surprise_pct) == (1.91, 1.9271, -0.8873)
    assert (latest.fiscal_year, latest.fiscal_quarter) == (2026, 3)


def test_finnhub_profile_and_unknown_symbol() -> None:
    server = Server(
        ok(fixture_json("finnhub/profile2.json")), ok(fixture_json("finnhub/profile2_unknown.json"))
    )
    client = finnhub(server)
    profile = client.profile("AAPL")
    assert profile is not None
    assert (profile.industry, profile.country, profile.ipo) == (
        "Technology",
        "US",
        date(1980, 12, 12),
    )
    assert client.profile("ZZZZQ") is None


def test_finnhub_unexpected_shapes() -> None:
    with pytest.raises(VendorError, match="unexpected response"):
        finnhub(Server(ok({"error": "x"}))).earnings_calendar(date(2026, 10, 20))
    with pytest.raises(VendorError, match="unexpected response"):
        finnhub(Server(ok({}))).earnings_surprises("AAPL")
    with pytest.raises(VendorError, match="unexpected response"):
        finnhub(Server(ok([]))).profile("AAPL")


def test_nasdaq_directory_both_files() -> None:
    server = Server(
        ok(fixture_text("nasdaq/nasdaqlisted.txt")), ok(fixture_text("nasdaq/otherlisted.txt"))
    )
    listings = {x.symbol: x for x in NasdaqDirectoryClient(client=server.client()).listings()}
    assert all(listings[s].is_etf for s in ("QQQ", "SPY", "DXJ"))
    assert not listings["AAPL"].is_etf
    assert not listings["VXX"].is_etf  # an ETN: the classifier's name rule catches it
    assert listings["ZXZZT"].is_test_issue
    assert listings["AAPL"].exchange == "NASDAQ"
    assert listings["BRK.B"].exchange == "NYSE"
    assert listings["SPY"].exchange == "NYSE Arca"
    assert listings["QDTE"].exchange == "Cboe BZX"
    assert not any(s.startswith("File Creation") for s in listings)
    assert [r.url.path for r in server.requests] == [
        "/dynamic/SymDir/nasdaqlisted.txt",
        "/dynamic/SymDir/otherlisted.txt",
    ]


def test_nasdaq_directory_with_a_changed_layout_is_an_error() -> None:
    with pytest.raises(VendorError, match="'ACT Symbol'"):
        parse_directory("Ticker|Security Name\nAAPL|Apple\n", nasdaq=False)

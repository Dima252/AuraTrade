from __future__ import annotations

import pytest

from auratrade.ingest.classify import (
    SecurityType,
    classify,
    description,
    refreshed,
    with_edgar,
    with_sic,
)

T = SecurityType


@pytest.mark.parametrize(
    ("symbol", "name", "expected"),
    [
        ("AAPL", "Apple Inc. - Common Stock", T.COMMON),
        ("BRK.B", "Berkshire Hathaway Inc. New Common Stock", T.COMMON),
        ("MOG.A", "Moog Inc. Class A Common Stock", T.COMMON),
        ("UNTC", "Unit Corporation - Common Stock", T.COMMON),
        ("O", "Realty Income Corporation Common Stock", T.COMMON),
        ("PEB", "Pebblebrook Hotel Trust Common Shares of Beneficial Interest", T.COMMON),
        ("ARE", "Alexandria Real Estate Equities, Inc. Common Stock", T.COMMON),
        ("SHOP", "Shopify Inc. Class A Subordinate Voting Shares", T.COMMON),
        ("TSM", "Taiwan Semiconductor Manufacturing Company Ltd.", T.COMMON),
        ("BABA", "Alibaba Group Holding Limited American Depositary Shares", T.ADR),
        ("ASML", "ASML Holding N.V. - New York Registry Shares", T.COMMON),
        ("PDD", "PDD Holdings Inc. - American Depositary Shares", T.ADR),
        ("NVO", "Novo Nordisk A/S Common Stock", T.COMMON),
        ("ARM", "Arm Holdings plc - American Depositary Shares", T.ADR),
        ("BAC.PRB", "Bank of America Corporation Depositary Shares Series GG", T.PREFERRED),
        ("PSA.PRK", "Public Storage Depositary Shares", T.PREFERRED),
        ("ABCD.U", "ABCD Acquisition Corp Units", T.UNIT),
        ("ABCD.WS", "ABCD Acquisition Corp Warrants", T.WARRANT),
        ("ABCD.WS.A", "ABCD Acquisition Corp Warrants Series A", T.WARRANT),
        ("ABCD.RT", "ABCD Acquisition Corp Rights", T.RIGHT),
        ("ABC.X1", "Odd symbol", T.OTHER),
        ("ABCDU", "ABCD Acquisition Corp - Units", T.UNIT),
        ("ABCDW", "ABCD Acquisition Corp - Warrant", T.WARRANT),
        ("ABCDR", "ABCD Acquisition Corp - Rights", T.RIGHT),
        ("ABCD", "ABCD Acquisition Corp - Class A Ordinary Shares", T.SPAC),
        ("MRGR", "Something Merger Corporation Class A Common Stock", T.SPAC),
        ("ARCC", "Ares Capital Corporation - Closed End Fund", T.FUND),
        ("MAIN", "Main Street Capital Corporation Common Stock", T.COMMON),
        ("ACMP", "Acme Corp - Depositary Shares each representing 1/1000th Preferred", T.PREFERRED),
        ("PFDX", "Some Bank 6.25% Pfd Series A", T.PREFERRED),
        ("NOTE", "Company 5.75% Senior Notes due 2029", T.NOTE),
        ("VXX", "iPath Series B S&P 500 VIX Short-Term Futures ETN", T.ETN),
        ("GAB", "Gabelli Equity Trust, Inc. (The) Common Stock", T.COMMON),
        ("ADX", "Adams Diversified Equity Fund, Inc.", T.FUND),
        ("PHYS", "Sprott Physical Gold Trust", T.FUND),
        ("MSCI", "MSCI Inc. Common Stock", T.COMMON),
    ],
)
def test_classify(symbol: str, name: str, expected: SecurityType) -> None:
    assert classify(symbol, name, is_etf=False) is expected


def test_directory_etf_flag_wins() -> None:
    assert classify("SPY", "State Street SPDR S&P 500 ETF Trust", is_etf=True) is T.ETF
    assert classify("DXJ", "WisdomTree Japan Hedged Equity Fund", is_etf=True) is T.ETF


def test_description() -> None:
    assert description("Apple Inc. - Common Stock") == "Common Stock"
    assert description("Berkshire Hathaway Inc. New Common Stock") == (
        "Berkshire Hathaway Inc. New Common Stock"
    )


def test_blank_check_sic() -> None:
    assert with_sic(T.COMMON, 6770) is T.SPAC
    assert with_sic(T.ADR, 6770) is T.SPAC
    assert with_sic(T.COMMON, 3674) is T.COMMON
    assert with_sic(T.COMMON, None) is T.COMMON
    assert with_sic(T.WARRANT, 6770) is T.WARRANT


def test_fund_filings() -> None:
    assert with_edgar(T.COMMON, None, {"N-CSR", "N-PX"}) is T.FUND
    assert with_edgar(T.COMMON, 6798, {"10-K", "10-Q"}) is T.COMMON
    assert with_edgar(T.COMMON, 6770, {"N-CSR"}) is T.SPAC
    assert with_edgar(T.ETF, None, {"NPORT-P"}) is T.ETF


def test_refreshed_keeps_edgar_verdicts() -> None:
    assert refreshed(T.COMMON, "FUND", None) is T.FUND
    assert refreshed(T.COMMON, "SPAC", None) is T.SPAC
    assert refreshed(T.WARRANT, "FUND", None) is T.WARRANT  # a new security on the symbol
    assert refreshed(T.COMMON, "COMMON", 6770) is T.SPAC
    assert refreshed(T.ADR, None, None) is T.ADR

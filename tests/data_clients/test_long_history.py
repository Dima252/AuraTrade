from __future__ import annotations

import io
import zipfile
from datetime import date

import httpx
import pytest

from auratrade.data.french import FRENCH_DAILY_URL, FrenchClient, parse_french_daily
from auratrade.data.http import VendorError
from auratrade.data.shiller import SHILLER_URL, ShillerClient, parse_shiller_csv
from tests.data_clients.fake import Server, fixture_text, ok

FRENCH_CSV = "french/F-F_Research_Data_Factors_daily.csv"


def zipped(*files: tuple[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files:
            archive.writestr(name, text)
    return buffer.getvalue()


def french(server: Server) -> FrenchClient:
    return FrenchClient(client=server.client(), sleep=lambda _: None)


def test_french_daily_reads_the_zipped_csv() -> None:
    body = zipped(("F-F_Research_Data_Factors_daily.csv", fixture_text(FRENCH_CSV)))
    server = Server(httpx.Response(200, content=body))
    daily = french(server).daily_factors()
    assert str(server.requests[0].url) == FRENCH_DAILY_URL
    assert daily.note == "This file was created by using the 202608 CRSP database."
    assert len(daily.days) == 11
    first, last = daily.days[0], daily.days[-1]
    # Mkt-RF 0.09% + RF 0.01%: the market's total return.
    assert (first.day, first.market, first.rf) == (date(1926, 7, 1), pytest.approx(0.001), 0.0001)
    assert (last.day, last.market) == (date(2026, 8, 31), pytest.approx(-0.0032))


def test_french_rejects_a_body_that_isnt_one_csv_in_a_zip() -> None:
    with pytest.raises(VendorError, match="not a zip"):
        french(Server(httpx.Response(200, content=b"<html>"))).daily_factors()
    two = zipped(("a.csv", "x"), ("b.CSV", "y"))
    with pytest.raises(VendorError, match="one CSV"):
        french(Server(httpx.Response(200, content=two))).daily_factors()


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("notes\n\nDate,Mkt,RF\n19260701,1,0\n", "no Mkt-RF header"),
        ("notes\n,Mkt-RF,SMB,HML,RF\n\nCopyright\n", "no rows"),
        ("n\n,Mkt-RF,SMB,HML,RF\n19260702,1,0,0,0\n19260701,1,0,0,0\n", "out of order"),
    ],
)
def test_french_malformed(text: str, error: str) -> None:
    with pytest.raises(VendorError, match=error):
        parse_french_daily(text)


def test_french_without_a_trailing_blank_line_reads_to_the_end() -> None:
    daily = parse_french_daily("n\n,Mkt-RF,SMB,HML,RF\n19260701,1,0,0,0\n19260702,2,0,0,0")
    assert [d.market for d in daily.days] == [0.01, 0.02]


def test_shiller_monthly_reads_prices_and_known_dividends() -> None:
    server = Server(ok(fixture_text("shiller/data.csv")))
    months = ShillerClient(client=server.client(), sleep=lambda _: None).monthly()
    assert str(server.requests[0].url) == SHILLER_URL
    assert len(months) == 9
    assert (months[0].month, months[0].price, months[0].dividend) == (date(1871, 1, 1), 4.44, 0.26)
    june = next(m for m in months if m.month == date(2023, 6, 1))
    assert june.dividend == 68.71
    # The CSV writes dividends not yet published as 0.0.
    assert [m.dividend for m in months[-4:]] == [None, None, None, None]
    assert months[-1].month == date(2026, 9, 1)


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("Month,Price\n1871-01-01,4.4\n", "columns"),
        ("Date,SP500,Dividend\n", "no rows"),
        ("Date,SP500,Dividend\n1871-01-15,4.4,0.2\n", "not the next month"),
        ("Date,SP500,Dividend\n1871-02-01,4.4,0.2\n1871-01-01,4.4,0.2\n", "not the next month"),
    ],
)
def test_shiller_malformed(text: str, error: str) -> None:
    with pytest.raises(VendorError, match=error):
        parse_shiller_csv(text)

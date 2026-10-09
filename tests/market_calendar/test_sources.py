from __future__ import annotations

import json
from datetime import UTC, date, datetime

import httpx
import pytest

from auratrade.calendar.sources import (
    AlpacaCalendarClient,
    AlpacaCalendarError,
    exchange_calendar_sessions,
    parse_alpaca_calendar,
)
from auratrade.calendar.sync import compare
from tests.calendar_fixture import FIXTURE, alpaca_times

KEY, SECRET = "PKTESTKEY", "test-secret-value"


def recorded() -> httpx.Response:
    return httpx.Response(200, content=FIXTURE.read_bytes())


class Server:
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(server: Server, sleeps: list[float] | None = None) -> AlpacaCalendarClient:
    return AlpacaCalendarClient(
        KEY,
        SECRET,
        client=httpx.Client(transport=httpx.MockTransport(server)),
        sleep=(sleeps if sleeps is not None else []).append,
    )


def test_parse_localizes_new_york_wall_clock() -> None:
    (s,) = parse_alpaca_calendar(
        [{"date": "2026-11-27", "open": "09:30", "close": "13:00", "session_open": "0400"}]
    )
    assert s.open_at == datetime(2026, 11, 27, 14, 30, tzinfo=UTC)
    assert s.close_at == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
    assert (s.is_early_close, s.minutes) == (True, 210)


def test_fetch_sends_keys_and_range() -> None:
    server = Server(recorded())
    sessions = client(server).fetch(date(2025, 12, 1), date(2027, 1, 31))
    (request,) = server.requests
    assert request.url.host == "paper-api.alpaca.markets"
    assert request.url.path == "/v2/calendar"
    assert dict(request.url.params) == {"start": "2025-12-01", "end": "2027-01-31"}
    assert request.headers["APCA-API-KEY-ID"] == KEY
    assert request.headers["APCA-API-SECRET-KEY"] == SECRET
    assert len(sessions) == 292
    assert [s.day for s in sessions] == sorted(s.day for s in sessions)


@pytest.mark.parametrize("first", [httpx.Response(503), httpx.Response(429)])
def test_retries_transient_errors(first: httpx.Response) -> None:
    sleeps: list[float] = []
    server = Server(first, httpx.ConnectError("reset"), recorded())
    assert client(server, sleeps).fetch(date(2026, 1, 1), date(2026, 12, 31))
    assert len(server.requests) == 3
    assert len(sleeps) == 2


def test_auth_error_is_not_retried_and_hides_keys() -> None:
    server = Server(httpx.Response(403, json={"message": "forbidden"}))
    with pytest.raises(AlpacaCalendarError, match="HTTP 403") as info:
        client(server).fetch(date(2026, 1, 1), date(2026, 12, 31))
    assert len(server.requests) == 1
    assert KEY not in str(info.value)
    assert SECRET not in str(info.value)


def test_unexpected_body() -> None:
    server = Server(httpx.Response(200, json={"error": "?"}))
    with pytest.raises(AlpacaCalendarError, match="unexpected"):
        client(server).fetch(date(2026, 1, 1), date(2026, 12, 31))


def test_transport_errors_give_up_after_four_attempts() -> None:
    server = Server(*[httpx.ReadTimeout("slow")] * 4)
    with pytest.raises(AlpacaCalendarError, match="ReadTimeout"):
        client(server).fetch(date(2026, 1, 1), date(2026, 12, 31))
    assert len(server.requests) == 4


def test_library_agrees_with_recorded_alpaca_calendar() -> None:
    # Spike 0.3c found exact agreement 2016-2027; this keeps checking it offline.
    library = exchange_calendar_sessions(date(2025, 12, 1), date(2027, 1, 29))
    assert compare(alpaca_times(), library) == []
    early = [s.day for s in library if s.is_early_close]
    assert early == [date(2025, 12, 24), date(2026, 11, 27), date(2026, 12, 24)]


def test_fixture_is_alpaca_shaped() -> None:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert set(rows[0]) >= {"date", "open", "close"}

from __future__ import annotations

import httpx
import pytest

from auratrade.data.http import RateLimiter, VendorError, VendorHttp
from tests.data_clients.fake import Server, ok

URL = "https://api.example.com/v1/series"
SECRET = "s3cr3t-api-key-value"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def http(server: Server, sleeps: list[float] | None = None) -> VendorHttp:
    return VendorHttp(
        "vendor",
        limiter=RateLimiter(per_second=100, sleep=lambda _: None),
        secrets=(SECRET,),
        client=server.client(),
        sleep=(sleeps if sleeps is not None else []).append,
    )


def test_limiter_waits_out_the_per_second_window() -> None:
    clock = Clock()
    limiter = RateLimiter(per_second=2, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        limiter.acquire()
    assert clock.slept == [pytest.approx(1.0)]


def test_limiter_applies_the_tighter_of_both_windows() -> None:
    clock = Clock()
    limiter = RateLimiter(per_second=10, per_minute=3, clock=clock, sleep=clock.sleep)
    for _ in range(4):
        limiter.acquire()
        clock.now += 0.01
    assert clock.slept == [pytest.approx(60.0 - 0.03)]


def test_limiter_without_limits_never_waits() -> None:
    clock = Clock()
    limiter = RateLimiter(clock=clock, sleep=clock.sleep)
    for _ in range(50):
        limiter.acquire()
    assert clock.slept == []


def test_drops_none_params_and_returns_json() -> None:
    server = Server(ok({"value": 1}))
    assert http(server).get_json(URL, {"a": "x", "b": None, "n": 3}) == {"value": 1}
    assert server.params() == {"a": "x", "n": "3"}


@pytest.mark.parametrize("first", [httpx.Response(429), httpx.Response(503)])
def test_retries_rate_limits_server_errors_and_transport_errors(first: httpx.Response) -> None:
    sleeps: list[float] = []
    server = Server(first, httpx.ReadTimeout("slow"), ok({"value": 2}))
    assert http(server, sleeps).get_json(URL) == {"value": 2}
    assert len(server.requests) == 3
    assert len(sleeps) == 2


def test_gives_up_after_the_last_attempt() -> None:
    server = Server(*[httpx.Response(500)] * 5)
    with pytest.raises(VendorError, match="HTTP 500") as info:
        http(server).get_json(URL)
    assert info.value.retryable
    assert len(server.requests) == 5


def test_client_errors_are_not_retried_and_never_show_the_url_or_key() -> None:
    body = {"error_message": f"The value {SECRET} for variable api_key is not registered."}
    server = Server(httpx.Response(400, json=body))
    with pytest.raises(VendorError) as info:
        http(server).get_json(URL, {"api_key": SECRET})
    message = str(info.value)
    assert message.startswith("vendor: /v1/series: HTTP 400: ")
    assert SECRET not in message
    assert "api_key=" not in message
    assert "example.com" not in message
    assert (info.value.status, info.value.retryable) == (400, False)
    assert len(server.requests) == 1


def test_transport_error_message_has_no_url() -> None:
    server = Server(*[httpx.ConnectError(f"cannot reach {URL}?api_key={SECRET}")] * 5)
    with pytest.raises(VendorError) as info:
        http(server).get_json(URL, {"api_key": SECRET})
    assert str(info.value) == "vendor: /v1/series: transport error ConnectError"


def test_404_is_an_error() -> None:
    server = Server(httpx.Response(404))
    with pytest.raises(VendorError, match="HTTP 404"):
        http(server).get_json(URL)
    assert len(server.requests) == 1  # not retried


def test_invalid_json_is_an_error() -> None:
    with pytest.raises(VendorError, match="invalid JSON"):
        http(Server(ok("<html>maintenance</html>"))).get_json(URL)


def test_get_text() -> None:
    assert http(Server(ok("a|b\n"))).get_text(URL) == "a|b\n"

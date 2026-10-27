from collections.abc import Callable
from typing import Any

import httpx
import pytest

from scripts import check_keys as ck

SECRETS = {
    "ALPACA_API_KEY": "PKALPACAKEY123",
    "ALPACA_SECRET_KEY": "alpaca-secret-456",
    "ANTHROPIC_API_KEY": "sk-ant-secret-789",
    "FRED_API_KEY": "fredkey0123456789",
    "TELEGRAM_BOT_TOKEN": "123456:telegramtoken",
    "DATABASE_URL": "postgresql+psycopg://user:dbpassword@db.example.net/app",
}
FULL_ENV = {
    **SECRETS,
    "TRADING_MODE": "dry_run",
    "TELEGRAM_CHAT_ID": "42",
}
OPTS = ck.Options()

Handler = Callable[[httpx.Request], httpx.Response]


def client(handler: Handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def json_response(body: Any, status: int = 200) -> Handler:
    return lambda request: httpx.Response(status, json=body)


@pytest.mark.parametrize(
    ("mode", "status"),
    [
        (None, ck.Status.OK),
        ("paper", ck.Status.OK),
        ("live", ck.Status.FAIL),
        ("x", ck.Status.FAIL),
    ],
)
def test_trading_mode(mode: str | None, status: ck.Status) -> None:
    env = {} if mode is None else {"TRADING_MODE": mode}
    assert ck.check_trading_mode(env, client(json_response({})), OPTS).status is status


@pytest.mark.parametrize("name", [n for n in ck.CHECKS if n != "trading_mode"])
def test_missing_vars_reported_without_calls(name: str) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    [result] = ck.run_checks({}, [name], client(fail), OPTS)
    assert result.status is ck.Status.MISSING


def test_fred_ok() -> None:
    body = {"observations": [{"date": "2026-10-07", "value": "16.2"}]}
    result = ck.check_fred(FULL_ENV, client(json_response(body)), OPTS)
    assert result == ck.Result("fred", ck.Status.OK, "VIXCLS 2026-10-07 = 16.2")


def test_fred_error_does_not_echo_body() -> None:
    body = {"error_message": f"api_key {SECRETS['FRED_API_KEY']} is not registered"}
    result = ck.check_fred(FULL_ENV, client(json_response(body, 400)), OPTS)
    assert result == ck.Result("fred", ck.Status.FAIL, "HTTP 400")


def test_alpaca_data_sends_keys_as_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"bars": {"SPY": {"t": "2026-10-07T19:59:00Z"}}})

    result = ck.check_alpaca_data(FULL_ENV, client(handler), OPTS)
    assert result.status is ck.Status.OK
    assert seen[0].headers["APCA-API-KEY-ID"] == SECRETS["ALPACA_API_KEY"]
    assert seen[0].url.params["feed"] == "iex"


def test_telegram_test_message_only_on_request() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"ok": True, "result": {"username": "aura_bot"}})

    assert ck.check_telegram(FULL_ENV, client(handler), OPTS).status is ck.Status.OK
    assert methods == ["getMe", "getChat"]
    methods.clear()
    ck.check_telegram(FULL_ENV, client(handler), ck.Options(telegram_test_message=True))
    assert methods == ["getMe", "getChat", "sendMessage"]


def test_redact_longest_first() -> None:
    assert ck.redact("abc-secret-long and abc-secret", ["abc-secret", "abc-secret-long"]) == (
        "*** and ***"
    )


def test_no_secret_reaches_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every check fails in a way that puts secrets in exception text; none may be printed."""

    def leaky(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(" ".join(SECRETS.values()))

    monkeypatch.setattr(ck, "make_trading_client", leaky)
    monkeypatch.setattr(ck, "make_anthropic_client", leaky)
    monkeypatch.setattr(ck.psycopg, "connect", leaky)

    def handler(request: httpx.Request) -> httpx.Response:
        if "fred" in request.url.host:
            raise httpx.ConnectError(f"failed {request.url}", request=request)
        return httpx.Response(500, text=" ".join(SECRETS.values()))

    results = ck.run_checks(FULL_ENV, ck.CHECKS, client(handler), OPTS)
    output = ck.format_results(results, FULL_ENV)

    assert all(r.status is not ck.Status.OK for r in results if r.name != "trading_mode")
    for secret in SECRETS.values():
        assert secret not in output

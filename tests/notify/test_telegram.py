import json

import httpx
import pytest

from auratrade.notify.alerts import LogAlerter
from auratrade.notify.telegram import MAX_MESSAGE_CHARS, TelegramError, TelegramNotifier

TOKEN = "123456:SECRET-token"


class Recorder:
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def notifier(recorder: Recorder, sleeps: list[float], clock: list[float] | None = None):  # type: ignore[no-untyped-def]
    ticks = clock if clock is not None else [0.0]
    return TelegramNotifier(
        TOKEN,
        "42",
        client=httpx.Client(transport=httpx.MockTransport(recorder)),
        sleep=sleeps.append,
        clock=lambda: ticks[0],
    )


def ok() -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})


def test_sends_message_to_chat() -> None:
    rec = Recorder(ok())
    notifier(rec, []).alert("ingest FAILED")
    (request,) = rec.requests
    assert request.url.path == f"/bot{TOKEN}/sendMessage"
    assert json.loads(request.content) == {
        "chat_id": "42",
        "text": "ingest FAILED",
        "disable_web_page_preview": True,
    }


def test_long_message_is_truncated() -> None:
    rec = Recorder(ok())
    notifier(rec, []).send("x" * (MAX_MESSAGE_CHARS + 50))
    text = json.loads(rec.requests[0].content)["text"]
    assert len(text) == MAX_MESSAGE_CHARS
    assert text.endswith("…")


@pytest.mark.parametrize("status", [429, 500, 502])
def test_retries_transient_errors(status: int) -> None:
    rec = Recorder(httpx.Response(status, json={"description": "busy"}), ok())
    sleeps: list[float] = []
    notifier(rec, sleeps).send("hi")
    assert len(rec.requests) == 2
    assert sleeps  # backed off before the retry


def test_retries_transport_errors_without_leaking_the_token() -> None:
    error = httpx.ConnectError(f"cannot reach https://api.telegram.org/bot{TOKEN}/sendMessage")
    rec = Recorder(error, error, error, error)
    with pytest.raises(TelegramError) as info:
        notifier(rec, []).send("hi")
    assert len(rec.requests) == 4
    assert TOKEN not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__


def test_client_error_is_not_retried_and_hides_the_url() -> None:
    rec = Recorder(httpx.Response(400, json={"description": "Bad Request: chat not found"}))
    with pytest.raises(TelegramError, match="chat not found") as info:
        notifier(rec, []).send("hi")
    assert len(rec.requests) == 1
    assert TOKEN not in str(info.value)
    assert not info.value.retryable


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]"])
def test_error_without_description(body: bytes) -> None:
    rec = Recorder(httpx.Response(403, content=body))
    with pytest.raises(TelegramError, match="HTTP 403: no description"):
        notifier(rec, []).send("hi")


def test_rate_limits_consecutive_messages() -> None:
    clock = [100.0]
    rec = Recorder(ok(), ok(), ok())
    sleeps: list[float] = []
    n = notifier(rec, sleeps, clock)
    n.send("one")
    clock[0] = 100.25
    n.send("two")
    assert sleeps == [pytest.approx(0.75)]
    clock[0] = 105.0
    n.send("three")
    assert len(sleeps) == 1


def test_log_alerter_does_not_raise() -> None:
    LogAlerter().alert("nothing configured")

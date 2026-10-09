"""Telegram alerts through the Bot API's `sendMessage`.

The bot token is part of the request URL, so no URL, request or httpx exception text is
ever logged or re-raised from here: errors carry only the status code and Telegram's own
description.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

API_URL = "https://api.telegram.org/bot{token}/sendMessage"
MAX_MESSAGE_CHARS = 4096
# Telegram allows about one message per second per chat.
MIN_INTERVAL_S = 1.0
TIMEOUT_S = 10.0


class TelegramError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, TelegramError) and exc.retryable


class _MinInterval:
    def __init__(self, interval_s: float, clock: Callable[[], float]) -> None:
        self._interval = interval_s
        self._clock = clock
        self._last: float | None = None
        self._lock = threading.Lock()

    def wait(self, sleep: Callable[[float], None]) -> None:
        with self._lock:
            now = self._clock()
            if self._last is not None and now - self._last < self._interval:
                sleep(self._interval - (now - self._last))
                now = self._clock()
            self._last = now


class TelegramNotifier:
    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = API_URL.format(token=token)
        self._chat_id = chat_id
        self._client = client or httpx.Client(timeout=TIMEOUT_S)
        self._sleep = sleep
        self._limiter = _MinInterval(MIN_INTERVAL_S, clock)

    def alert(self, text: str) -> None:
        self.send(text)

    def send(self, text: str) -> None:
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[: MAX_MESSAGE_CHARS - 1] + "…"
        self._send_with_retry(text)

    def _send_with_retry(self, text: str) -> None:
        @retry(
            retry=retry_if_exception(_is_retryable),
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=1, max=10),
            sleep=self._sleep,
            reraise=True,
        )
        def attempt() -> None:
            self._limiter.wait(self._sleep)
            self._post(text)

        attempt()

    def _post(self, text: str) -> None:
        try:
            response = self._client.post(
                self._url,
                json={"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True},
            )
        except httpx.TransportError as exc:
            raise TelegramError(f"transport error: {type(exc).__name__}", retryable=True) from None
        if response.status_code == httpx.codes.OK:
            return
        description = _description(response)
        retryable = response.status_code == httpx.codes.TOO_MANY_REQUESTS or (
            response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR
        )
        raise TelegramError(f"HTTP {response.status_code}: {description}", retryable=retryable)


def _description(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "no description"
    if isinstance(body, dict):
        return str(body.get("description", "no description"))
    return "no description"

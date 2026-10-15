"""Shared HTTP for the vendor clients: per-vendor rate limits, retries, safe errors.

Error messages never contain a URL: FRED only accepts its key as a query parameter, and
httpx puts the full URL in its exception messages (spike 0.3d). They name the vendor, the
path and the status, and every known secret is redacted from them as well.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from auratrade.log import Redactor

DEFAULT_TIMEOUT_S = 60.0
ATTEMPTS = 5
# Vendors put short reasons in their error bodies; more would only fill the logs.
MAX_DETAIL_CHARS = 200

Params = Mapping[str, str | int | float | bool | None]


class VendorError(Exception):
    def __init__(
        self, vendor: str, message: str, *, retryable: bool, status: int | None = None
    ) -> None:
        super().__init__(f"{vendor}: {message}")
        self.vendor = vendor
        self.retryable = retryable
        self.status = status


def _retryable(exc: BaseException) -> bool:
    return isinstance(exc, VendorError) and exc.retryable


class RateLimiter:
    """Sliding windows of at most `per_second` and `per_minute` requests. Thread-safe, so
    parallel backfill workers can share one limiter per vendor account."""

    def __init__(
        self,
        *,
        per_second: int | None = None,
        per_minute: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._windows = [(n, w) for n, w in ((per_second, 1.0), (per_minute, 60.0)) if n]
        self._sent: list[deque[float]] = [deque() for _ in self._windows]
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while (wait := self._wait()) > 0:
                self._sleep(wait)
            now = self._clock()
            for sent in self._sent:
                sent.append(now)

    def _wait(self) -> float:
        now = self._clock()
        wait = 0.0
        for (limit, window), sent in zip(self._windows, self._sent, strict=True):
            while sent and now - sent[0] >= window:
                sent.popleft()
            if len(sent) >= limit:
                wait = max(wait, window - (now - sent[0]))
        return wait


class VendorHttp:
    """GET with rate limiting and retries for one vendor. `client` and `sleep` are injected
    in tests (an `httpx.MockTransport` and a recorder)."""

    def __init__(
        self,
        vendor: str,
        *,
        limiter: RateLimiter,
        headers: Mapping[str, str] | None = None,
        secrets: Iterable[str] = (),
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        attempts: int = ATTEMPTS,
    ) -> None:
        self.vendor = vendor
        self._limiter = limiter
        self._headers = dict(headers or {})
        self._redact = Redactor(secrets)
        self._client = client or httpx.Client(timeout=DEFAULT_TIMEOUT_S, follow_redirects=True)
        self._sleep = sleep
        self._attempts = attempts

    def get(self, url: str, params: Params | None = None) -> httpx.Response:
        """The response for a 2xx; anything else raises `VendorError`."""

        @retry(
            retry=retry_if_exception(_retryable),
            stop=stop_after_attempt(self._attempts),
            wait=wait_exponential(multiplier=2, max=30),
            sleep=self._sleep,
            reraise=True,
        )
        def attempt() -> httpx.Response:
            return self._once(url, params)

        return attempt()

    def get_json(self, url: str, params: Params | None = None) -> Any:
        return self._json(url, self.get(url, params))

    def get_json_or_none(self, url: str, params: Params | None = None) -> Any:
        """As `get_json`, but None for a 404 (never retried)."""
        try:
            return self.get_json(url, params)
        except VendorError as exc:
            if exc.status == httpx.codes.NOT_FOUND:
                return None
            raise

    def get_text(self, url: str, params: Params | None = None) -> str:
        return self.get(url, params).text

    def _once(self, url: str, params: Params | None) -> httpx.Response:
        path = httpx.URL(url).path
        query = {k: v for k, v in (params or {}).items() if v is not None}
        self._limiter.acquire()
        try:
            response = self._client.get(url, params=query, headers=self._headers)
        except httpx.TransportError as exc:
            raise VendorError(
                self.vendor, f"{path}: transport error {type(exc).__name__}", retryable=True
            ) from None
        status = response.status_code
        if status < 300:
            return response
        retryable = status == httpx.codes.TOO_MANY_REQUESTS or status >= 500
        detail = self._redact(response.text[:MAX_DETAIL_CHARS].strip())
        message = f"{path}: HTTP {status}" + (f": {detail}" if detail else "")
        raise VendorError(self.vendor, message, retryable=retryable, status=status)

    def _json(self, url: str, response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            path = httpx.URL(url).path
            raise VendorError(self.vendor, f"{path}: invalid JSON", retryable=False) from None

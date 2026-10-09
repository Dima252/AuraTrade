"""structlog setup: JSON lines, UTC timestamps, secrets redacted (CLAUDE.md §3.2)."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any, TextIO

import structlog

REDACTED = "***"


class Redactor:
    """Replaces every known secret value in a string."""

    def __init__(self, secrets: Iterable[str]) -> None:
        # Longest first, so a secret that contains another is replaced whole.
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text

    def processor(
        self, _logger: object, _method: str, event_dict: MutableMapping[str, Any]
    ) -> Mapping[str, Any]:
        return {key: self._scrub(value) for key, value in event_dict.items()}

    def _scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            return self(value)
        if isinstance(value, Mapping):
            return {k: self._scrub(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self._scrub(v) for v in value]
        if isinstance(value, BaseException):
            return self(f"{type(value).__name__}: {value}")
        return value


def configure_logging(
    secrets: Iterable[str] = (), *, level: int = logging.INFO, stream: TextIO | None = None
) -> Redactor:
    redactor = Redactor(secrets)
    out = stream if stream is not None else sys.stdout
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            redactor.processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(out),
        cache_logger_on_first_use=False,
    )
    # httpx logs every request URL at INFO, and Telegram's URL contains the bot token.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return redactor

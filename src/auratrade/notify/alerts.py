"""The alert interface every job uses, and a fallback that only logs."""

from __future__ import annotations

from typing import Protocol

import structlog

log = structlog.get_logger(__name__)


class Alerter(Protocol):
    def alert(self, text: str) -> None:
        """Deliver `text` to the operator. May raise; callers must not let it mask errors."""


class LogAlerter:
    """Used when Telegram isn't configured: the alert still reaches the logs."""

    def alert(self, text: str) -> None:
        log.error("alert", text=text, delivered=False)

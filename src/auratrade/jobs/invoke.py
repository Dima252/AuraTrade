"""How a chained job starts its successor: an async Lambda invoke in AWS, in-process locally."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from typing import TYPE_CHECKING, Protocol

import structlog

if TYPE_CHECKING:
    from mypy_boto3_lambda import LambdaClient

log = structlog.get_logger(__name__)


class JobInvoker(Protocol):
    def invoke(self, job: str, session_date: date, *, dry_run: bool) -> None: ...


def invocation_payload(session_date: date, *, dry_run: bool) -> dict[str, object]:
    return {"session": session_date.isoformat(), "dry_run": dry_run}


class LambdaInvoker:
    def __init__(self, client: LambdaClient, *, function_template: str, environment: str) -> None:
        self._client = client
        self._template = function_template
        self._environment = environment

    def function_name(self, job: str) -> str:
        return self._template.format(env=self._environment, job=job)

    def invoke(self, job: str, session_date: date, *, dry_run: bool) -> None:
        payload = invocation_payload(session_date, dry_run=dry_run)
        # "Event" is asynchronous: this job finishes now, and Lambda retries the successor
        # on its own if it errors.
        self._client.invoke(
            FunctionName=self.function_name(job),
            InvocationType="Event",
            Payload=json.dumps(payload).encode(),
        )


class LocalInvoker:
    """Runs the successor in this process (`--chain` on the CLI)."""

    def __init__(self, run: Callable[[str, date, bool], object]) -> None:
        self._run = run

    def invoke(self, job: str, session_date: date, *, dry_run: bool) -> None:
        self._run(job, session_date, dry_run)


class NoChainInvoker:
    """The default for local runs: report the successor instead of running it."""

    def invoke(self, job: str, session_date: date, *, dry_run: bool) -> None:
        log.info("chain_not_followed", successor=job, session=session_date.isoformat())

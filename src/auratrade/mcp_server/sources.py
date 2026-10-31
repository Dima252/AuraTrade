"""What the MCP server reads, each read-only below the code (README §5.10, #37).

- **The database:** every connection is made read-only before first use, so Postgres itself
  refuses a write, whatever a query says.
- **The broker:** typed as a `BrokerReader` (the account, positions, open orders); the order
  calls aren't on the type, so a write doesn't type-check.
- **The logs:** CloudWatch's `FilterLogEvents`, nothing else.
- **The snapshot:** the dashboard's allow-listed file (§5.8), from the site, its bucket (the
  remote server, whose role can read that one object) or a local file.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any, Protocol

import httpx
from sqlalchemy import Engine, event

from auratrade.db.engine import make_engine
from auratrade.execution.broker import Account, BrokerOrder, BrokerPosition

READ_ONLY = "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"


class SourceError(Exception):
    """A source is unreachable or empty: the tool's answer says so, nothing more."""


class BrokerReader(Protocol):
    def account(self) -> Account: ...

    def is_market_open(self) -> bool: ...

    def positions(self) -> list[BrokerPosition]: ...

    def open_orders(self) -> list[BrokerOrder]: ...


def read_only_engine(url: str) -> Engine:
    engine = make_engine(url)

    @event.listens_for(engine, "connect")
    def _read_only(dbapi_connection: Any, _record: object) -> None:
        # SET is transactional in Postgres: committed, it holds for the connection's life.
        with dbapi_connection.cursor() as cursor:
            cursor.execute(READ_ONLY)
        dbapi_connection.commit()

    return engine


class Snapshot:
    """The dashboard's snapshot from an https URL (the site), an `s3://bucket/key` (the site's
    bucket) or a local file."""

    def __init__(
        self,
        location: str | None,
        *,
        http: Callable[[str], httpx.Response] | None = None,
        s3: Callable[[str, str], bytes | None] | None = None,
        max_age: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._location = location
        self._http = http or (lambda url: httpx.get(url, timeout=10.0, follow_redirects=True))
        self._s3 = s3 or _s3_object
        self._max_age = max_age
        self._clock = clock
        self._kept: tuple[float, Mapping[str, Any]] | None = None

    def read(self) -> Mapping[str, Any]:
        """The snapshot, or the last one read within `max_age` seconds (a remote server under
        load shouldn't fetch an evening's file on every call)."""
        now = self._clock()
        if self._kept is not None and now - self._kept[0] < self._max_age:
            return self._kept[1]
        data = self._fetch()
        self._kept = (now, data)
        return data

    def _fetch(self) -> Mapping[str, Any]:
        where = self._location
        if where is None:
            raise SourceError("no snapshot location: start the server with --snapshot")
        if where.startswith(("https://", "http://")):
            response = self._http(where)
            if response.status_code in (403, 404):
                raise SourceError("no snapshot published yet: the trial's first evening writes it")
            if response.status_code != 200:
                raise SourceError(f"the snapshot's site answered HTTP {response.status_code}")
            data = response.json()
        elif where.startswith("s3://"):
            bucket, _, key = where.removeprefix("s3://").partition("/")
            body = self._s3(bucket, key)
            if body is None:
                raise SourceError("no snapshot published yet: the trial's first evening writes it")
            data = json.loads(body)
        else:
            path = Path(where)
            if not path.is_file():
                raise SourceError(f"no snapshot file at {path}")
            data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise SourceError("the snapshot isn't a JSON object")
        return data


@cache
def _s3_client() -> Any:
    import boto3

    return boto3.client("s3")


def _s3_object(bucket: str, key: str) -> bytes | None:
    """The object's bytes, or None when it isn't there. Without ListBucket (the role reads one
    key) S3 answers a missing key with AccessDenied, as CloudFront answers 403."""
    from botocore.exceptions import ClientError

    try:
        body: bytes = _s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "AccessDenied"):
            return None
        raise
    return body


# Lambda's own lines worth an operator's eye, besides the jobs' warnings and errors.
PLATFORM_PROBLEMS = ("Task timed out", "Status: timeout", "Status: error", "Runtime.", "Traceback")
QUIET_LEVELS = frozenset({"debug", "info"})


class LogReader:
    """A job's CloudWatch events (`/aws/lambda/<function>`), read with `FilterLogEvents`."""

    def __init__(
        self, client: Callable[[], Any], *, function_template: str, environment: str
    ) -> None:
        self._client = client
        self._template = function_template
        self._environment = environment

    def group(self, job: str) -> str:
        return "/aws/lambda/" + self._template.format(env=self._environment, job=job)

    def events(
        self, job: str, since: datetime, *, everything: bool, limit: int
    ) -> list[dict[str, Any]]:
        client = self._client()
        params: dict[str, Any] = {
            "logGroupName": self.group(job),
            "startTime": int(since.timestamp() * 1000),
        }
        out: list[dict[str, Any]] = []
        while True:
            page = client.filter_log_events(**params)
            for raw in page.get("events", []):
                line = parse_line(raw["timestamp"], raw["message"])
                if everything or is_problem(line):
                    out.append(line)
            token = page.get("nextToken")
            if token is None or token == params.get("nextToken"):
                break
            params["nextToken"] = token
        return out[-limit:]


def parse_line(timestamp_ms: int, message: str) -> dict[str, Any]:
    at = datetime.fromtimestamp(timestamp_ms / 1000, UTC).isoformat()
    text = message.strip()
    try:
        record = json.loads(text)
    except ValueError:
        return {"at": at, "level": None, "text": text}
    if not isinstance(record, dict):
        return {"at": at, "level": None, "text": text}
    record.pop("timestamp", None)
    level = record.pop("level", None)
    return {"at": at, "level": level, "event": record.pop("event", None), "fields": record}


def is_problem(line: Mapping[str, Any]) -> bool:
    level = line.get("level")
    if level is not None:
        return str(level).lower() not in QUIET_LEVELS
    return any(mark in str(line.get("text", "")) for mark in PLATFORM_PROBLEMS)

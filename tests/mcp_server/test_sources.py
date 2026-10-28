import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import DBAPIError

from auratrade.db.models import SystemFlag
from auratrade.mcp_server.sources import (
    LogReader,
    Snapshot,
    SourceError,
    is_problem,
    parse_line,
    read_only_engine,
)

URL = "https://site.example/data/snapshot.json"


def _http(status: int, body: object = None) -> Any:
    def get(url: str) -> httpx.Response:
        assert url == URL
        return httpx.Response(status, json=body, request=httpx.Request("GET", url))

    return get


def test_the_snapshot_from_the_site() -> None:
    assert Snapshot(URL, http=_http(200, {"through": "2026-12-04"})).read() == {
        "through": "2026-12-04"
    }


@pytest.mark.parametrize("status", [403, 404])
def test_before_the_first_snapshot(status: int) -> None:
    # CloudFront answers 403 for a missing key in a private bucket.
    with pytest.raises(SourceError, match="no snapshot published yet"):
        Snapshot(URL, http=_http(status)).read()


def test_a_site_error() -> None:
    with pytest.raises(SourceError, match="HTTP 503"):
        Snapshot(URL, http=_http(503)).read()


def test_not_an_object() -> None:
    with pytest.raises(SourceError, match="isn't a JSON object"):
        Snapshot(URL, http=_http(200, [1, 2])).read()


def test_the_snapshot_from_a_file(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"mode": "paper"}), encoding="utf-8")

    assert Snapshot(str(path)).read() == {"mode": "paper"}
    with pytest.raises(SourceError, match="no snapshot file"):
        Snapshot(str(tmp_path / "missing.json")).read()


def test_no_snapshot_location() -> None:
    with pytest.raises(SourceError, match="--snapshot"):
        Snapshot(None).read()


def test_a_structlog_line() -> None:
    line = parse_line(
        1_793_218_879_000,
        '{"job": "ingest", "event": "macro_source_failed", "level": "warning", '
        '"timestamp": "2026-10-28T20:21:19Z"}\n',
    )

    assert line == {
        "at": "2026-10-28T20:21:19+00:00",
        "level": "warning",
        "event": "macro_source_failed",
        "fields": {"job": "ingest"},
    }
    assert is_problem(line)
    assert not is_problem({"level": "info"})


def test_lambda_platform_lines() -> None:
    timeout = parse_line(0, "INIT_REPORT Init Duration: 9999.44 ms\tPhase: init\tStatus: timeout")
    report = parse_line(0, "REPORT RequestId: abc\tDuration: 3.2 ms")

    assert timeout["level"] is None
    assert is_problem(timeout)
    assert not is_problem(report)
    assert parse_line(0, "[1, 2]")["text"] == "[1, 2]"


class _Logs:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def filter_log_events(self, **params: Any) -> dict[str, Any]:
        self.calls.append(params)
        return self.pages[(len(self.calls) - 1) % len(self.pages)]


def _event(level: str, event: str) -> dict[str, Any]:
    return {"timestamp": 0, "message": json.dumps({"level": level, "event": event})}


def test_log_events_paged_and_filtered() -> None:
    client = _Logs(
        [
            {"events": [_event("info", "job_start"), _event("error", "boom")], "nextToken": "t1"},
            {"events": [_event("warning", "slow")], "nextToken": "t1"},
        ]
    )
    reader = LogReader(
        lambda: client, function_template="auratrade-{env}-{job}", environment="paper"
    )
    since = datetime(2026, 10, 28, 13, 0, tzinfo=UTC)

    problems = reader.events("ingest", since, everything=False, limit=10)
    every = reader.events("ingest", since, everything=True, limit=2)

    assert [e["event"] for e in problems] == ["boom", "slow"]
    assert [e["event"] for e in every] == ["boom", "slow"]  # the last `limit`
    assert client.calls[0] == {
        "logGroupName": "/aws/lambda/auratrade-paper-ingest",
        "startTime": 1_793_192_400_000,
    }
    assert client.calls[1]["nextToken"] == "t1"


def test_the_database_refuses_writes(migrated_url: str, pg_engine: object) -> None:
    engine = read_only_engine(migrated_url)
    try:
        with engine.connect() as conn:
            assert conn.scalar(text("SHOW default_transaction_read_only")) == "on"
            assert conn.execute(select(SystemFlag.name)).all()
        with pytest.raises(DBAPIError, match="read-only transaction"), engine.begin() as conn:
            conn.execute(insert(SystemFlag).values(name="mcp_probe", value=True))
    finally:
        engine.dispose()

"""The server end to end through the SDK's in-memory client (README §5.10)."""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from mcp import Client
from sqlalchemy import Engine

from auratrade.config import load_risk_config
from auratrade.log import Redactor
from auratrade.mcp_server.server import Operator, ServerDeps, build_server
from auratrade.mcp_server.sources import LogReader, Snapshot, SourceError, read_only_engine
from tests.mcp_server.snapshots import snapshot
from tests.mcp_server.test_operator import Reader

PUBLIC_TOOLS = {"performance", "decisions", "exposure", "scorecard"}
OPERATOR_TOOLS = {"status", "jobs", "logs", "portfolio", "trend", "tax", "decision"}
NOW = datetime(2026, 10, 28, 17, 0, tzinfo=UTC)
README = (
    "## 5. Components\n\n### 5.4 Portfolio Manager\n\nThe LLM.\n\n"
    "## 16. Decision log\n\n| 174 | The MCP server (6.4) | Read-only. |\n"
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _Snapshot(Snapshot):
    def __init__(self, doc: dict[str, Any] | None) -> None:
        super().__init__("https://site.example/data/snapshot.json")
        self.doc = doc

    def read(self) -> dict[str, Any]:
        if self.doc is None:
            raise SourceError("no snapshot published yet: the trial's first evening writes it")
        return self.doc


def _public(doc: dict[str, Any] | None = None) -> ServerDeps:
    return ServerDeps(
        snapshot=_Snapshot(snapshot() if doc is None else doc),
        redact=Redactor(["s3cret"]),
        now=lambda: NOW,
        readme=lambda: README,
    )


class _Logs:
    def filter_log_events(self, **params: Any) -> dict[str, Any]:
        return {"events": [{"timestamp": 0, "message": '{"level": "error", "event": "boom"}'}]}


@pytest.fixture
def operator_deps(pg_engine: Engine, migrated_url: str) -> Iterator[ServerDeps]:
    engine = read_only_engine(migrated_url)
    op = Operator(
        engine=engine,
        risk=load_risk_config(),
        broker=lambda: Reader(),
        logs=LogReader(
            lambda: _Logs(), function_template="auratrade-{env}-{job}", environment="paper"
        ),
        environment="paper",
    )
    yield ServerDeps(
        snapshot=_Snapshot(snapshot()), redact=Redactor(["s3cret"]), now=lambda: NOW, operator=op
    )
    engine.dispose()


@pytest.mark.anyio
async def test_the_public_server_serves_the_snapshot_tools_only() -> None:
    async with Client(build_server(_public())) as client:
        tools = (await client.list_tools()).tools
        prompts = {p.name for p in (await client.list_prompts()).prompts}

    assert {t.name for t in tools} == PUBLIC_TOOLS
    assert prompts == {"monthly_review"}


@pytest.mark.anyio
async def test_every_tool_is_read_only(operator_deps: ServerDeps) -> None:
    async with Client(build_server(operator_deps)) as client:
        tools = (await client.list_tools()).tools

    assert {t.name for t in tools} == PUBLIC_TOOLS | OPERATOR_TOOLS
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.destructive_hint is False, tool.name
        assert tool.description, tool.name


@pytest.mark.anyio
async def test_performance_through_the_protocol() -> None:
    async with Client(build_server(_public())) as client:
        result = await client.call_tool("performance", {"since": "2026-11-03"})

    assert not result.is_error
    assert result.structured_content is not None
    assert result.structured_content["sessions"] == 2


@pytest.mark.anyio
async def test_before_the_first_snapshot_the_tool_says_so() -> None:
    deps = _public()
    assert isinstance(deps.snapshot, _Snapshot)
    deps.snapshot.doc = None
    async with Client(build_server(deps)) as client:
        result = await client.call_tool("decisions", {})

    assert result.is_error
    assert "no snapshot published yet" in result.content[0].text  # type: ignore[union-attr]


@pytest.mark.anyio
async def test_an_expected_failure_is_redacted() -> None:
    class Leaky(_Snapshot):
        def read(self) -> dict[str, Any]:
            raise httpx.ConnectError("token=s3cret refused")

    deps = _public()
    deps = ServerDeps(snapshot=Leaky(None), redact=deps.redact, now=deps.now)
    async with Client(build_server(deps)) as client:
        result = await client.call_tool("scorecard", {})

    text = result.content[0].text  # type: ignore[union-attr]
    assert result.is_error
    assert "s3cret" not in text
    assert "token=***" in text


@pytest.mark.anyio
async def test_an_unexpected_failure_shows_the_tool_name_only() -> None:
    class Broken(_Snapshot):
        def read(self) -> dict[str, Any]:
            raise RuntimeError("internal detail s3cret")

    deps = _public()
    deps = ServerDeps(snapshot=Broken(None), redact=deps.redact, now=deps.now)
    async with Client(build_server(deps)) as client:
        result = await client.call_tool("exposure", {})

    assert result.is_error
    assert "s3cret" not in result.content[0].text  # type: ignore[union-attr]


@pytest.mark.anyio
async def test_the_operator_tools_answer(operator_deps: ServerDeps) -> None:
    async with Client(build_server(operator_deps)) as client:
        status = await client.call_tool("status", {})
        jobs = await client.call_tool("jobs", {"job": "ingest"})
        portfolio = await client.call_tool("portfolio", {})
        trend = await client.call_tool("trend", {})
        tax = await client.call_tool("tax", {})
        decision = await client.call_tool("decision", {})
        logs = await client.call_tool("logs", {"job": "ingest", "hours": 2})

    for result in (status, jobs, portfolio, trend, tax, decision, logs):
        assert not result.is_error, result.content
    assert status.structured_content is not None
    assert status.structured_content["kill_switch"] == "ON (increases blocked)"  # a fresh DB
    assert tax.structured_content is not None
    assert tax.structured_content["year"] == 2026
    assert logs.structured_content is not None
    assert logs.structured_content["log_group"] == "/aws/lambda/auratrade-paper-ingest"
    assert logs.structured_content["since"] == "2026-10-28T15:00:00+00:00"
    assert [e["event"] for e in logs.structured_content["events"]] == ["boom"]


@pytest.mark.anyio
async def test_logs_refuses_a_bad_job_name(operator_deps: ServerDeps) -> None:
    async with Client(build_server(operator_deps)) as client:
        result = await client.call_tool("logs", {"job": "../../etc"})

    assert result.is_error
    assert "not a job name" in result.content[0].text  # type: ignore[union-attr]


@pytest.mark.anyio
async def test_logs_without_cloudwatch(operator_deps: ServerDeps) -> None:
    assert operator_deps.operator is not None
    op = Operator(**{**operator_deps.operator.__dict__, "logs": None, "broker": None})
    deps = ServerDeps(
        snapshot=operator_deps.snapshot, redact=operator_deps.redact, now=lambda: NOW, operator=op
    )
    async with Client(build_server(deps)) as client:
        logs = await client.call_tool("logs", {"job": "ingest"})
        portfolio = await client.call_tool("portfolio", {})

    assert logs.is_error
    assert portfolio.structured_content is not None
    assert portfolio.structured_content["broker"] is None


@pytest.mark.anyio
async def test_the_documents() -> None:
    async with Client(build_server(_public())) as client:
        index = await client.read_resource("auratrade://readme")
        section = await client.read_resource("auratrade://readme/5.4")
        entry = await client.read_resource("auratrade://decision/174")
        decisions = await client.read_resource("auratrade://decisions")
        runbook = await client.read_resource("auratrade://runbook")
        backtest = await client.read_resource("auratrade://backtest")
        review = await client.get_prompt("monthly_review", {})

    assert "5.4 Portfolio Manager" in index.contents[0].text  # type: ignore[union-attr]
    assert section.contents[0].text.endswith("The LLM.")  # type: ignore[union-attr]
    assert entry.contents[0].text.startswith("# Decision #174")  # type: ignore[union-attr]
    assert "#174" in decisions.contents[0].text  # type: ignore[union-attr]
    assert "runbook" in runbook.contents[0].text.lower()  # type: ignore[union-attr]
    assert "portfolio" in backtest.contents[0].text.lower()  # type: ignore[union-attr]
    review_text = review.messages[0].content.text  # type: ignore[union-attr]
    assert "the latest complete month" in review_text
    assert "`tax`" not in review_text  # the public server has no operator tools


@pytest.mark.anyio
@pytest.mark.parametrize(
    "uri", ["auratrade://readme/9.9", "auratrade://decision/1", "auratrade://decision/abc"]
)
async def test_a_missing_document(uri: str) -> None:
    from mcp.shared.exceptions import MCPError

    async with Client(build_server(_public())) as client:
        with pytest.raises(MCPError):
            await client.read_resource(uri)


@pytest.mark.anyio
async def test_the_prompts(operator_deps: ServerDeps) -> None:
    async with Client(build_server(operator_deps)) as client:
        review = await client.get_prompt("monthly_review", {"month": "2026-11"})
        triage = await client.get_prompt("triage_alert", {"alert": "ingest 2026-10-28 FAILED"})

    review_text = review.messages[0].content.text  # type: ignore[union-attr]
    triage_text = triage.messages[0].content.text  # type: ignore[union-attr]
    assert "the decision executed in 2026-11" in review_text
    assert "`decision`, `tax`, `jobs`" in review_text
    assert "<alert>\ningest 2026-10-28 FAILED\n</alert>" in triage_text
    assert "auratrade://runbook" in triage_text

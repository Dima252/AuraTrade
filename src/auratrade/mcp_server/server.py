"""The MCP server's tools, resources and prompts (README §5.10, #37).

Strictly read-only: every tool reads (the snapshot, the read-only database, the broker as a
`BrokerReader`, CloudWatch's `FilterLogEvents`) and carries the read-only hint. Without the
operator's sources (`--public`) only the snapshot's tools are served.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from botocore.exceptions import BotoCoreError, ClientError
from mcp.server.apps import Apps
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from auratrade.config import RiskConfig
from auratrade.execution.broker import BrokerError
from auratrade.mcp_server import app, docs, operator, public
from auratrade.mcp_server.sources import BrokerReader, LogReader, Snapshot, SourceError

INSTRUCTIONS = """\
AuraTrade is an autonomous portfolio manager on an Alpaca paper account ($100,000 at the start \
of the trial). A trend rule sets the S&P 500 exposure (1.3x while the trend is up, 0.5x while \
down); an LLM allocates a 20% satellite among approved ETFs and may shift the level by ±0.2x, \
inside limits enforced by code. A rules-only twin runs beside it as the LLM's benchmark, and \
results are judged after the estimated Israeli tax (25%).

This server is read-only: it can't trade, change a flag or write anything. Start with \
`performance` or `status`; `dashboard` draws the book as a page where the client supports MCP \
Apps. The README and the decision log are resources (auratrade://readme, \
auratrade://decisions) for why the system works as it does."""

READ = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
JOB_NAME = re.compile(r"^[a-z_]{1,40}$")
EXPECTED = (SourceError, SQLAlchemyError, BrokerError, BotoCoreError, ClientError, httpx.HTTPError)


@dataclass(frozen=True)
class Operator:
    """The operator's sources (local only)."""

    engine: Engine
    risk: RiskConfig
    broker: Callable[[], BrokerReader] | None
    logs: LogReader | None
    environment: str


@dataclass(frozen=True)
class ServerDeps:
    snapshot: Snapshot
    redact: Callable[[str], str]
    now: Callable[[], datetime]
    """Timezone-aware, New York."""
    operator: Operator | None = None
    readme: Callable[[], str] = lambda: docs.read(docs.README)


def _answer[T](deps: ServerDeps, call: Callable[[], T]) -> T:
    """An expected failure (a source down, credentials expired) as a redacted message the model
    can act on; anything else stays a crash, which the client sees as the tool's name only."""
    try:
        return call()
    except SourceError as exc:
        raise ToolError(deps.redact(str(exc))) from exc
    except EXPECTED as exc:
        raise ToolError(deps.redact(f"{type(exc).__name__}: {exc}")) from exc


def build_server(deps: ServerDeps) -> MCPServer:
    server = MCPServer(
        name="auratrade", title="AuraTrade", instructions=INSTRUCTIONS, extensions=[_apps(deps)]
    )
    _public_tools(server, deps)
    if deps.operator is not None:
        _operator_tools(server, deps, deps.operator)
    _resources(server, deps)
    _prompts(server, deps)
    return server


def _apps(deps: ServerDeps) -> Apps:
    """The `dashboard` tool and its page (#39): the page draws `structuredContent`,
    which hosts keep out of the model's context; the text is what the model reads and what a
    client without MCP Apps shows."""
    apps = Apps()

    @apps.tool(resource_uri=app.URI, title="AuraTrade dashboard", annotations=READ)
    def dashboard() -> CallToolResult:
        """The book at a glance, drawn as a dashboard where the client supports MCP Apps: the
        paper account's balance, its gain and the estimated tax, the book against the twin and
        SPY before and after tax, the exposure, the satellite and the latest decision. Clients
        without MCP Apps get the same as a short text."""
        doc = _answer(deps, deps.snapshot.read)
        return CallToolResult(
            content=[TextContent(type="text", text=public.dashboard_text(doc))],
            structured_content=public.dashboard(doc),
        )

    apps.add_html_resource(
        app.URI,
        app.html(),
        name="dashboard",
        title="AuraTrade dashboard",
        description="The `dashboard` tool's page: the book, the tax, the exposure, the decision.",
        prefers_border=False,
    )
    return apps


def _public_tools(server: MCPServer, deps: ServerDeps) -> None:
    def snapshot() -> Any:
        return deps.snapshot.read()

    @server.tool(annotations=READ)
    def performance(since: date | None = None) -> dict[str, Any]:
        """The book (the LLM's decisions), the twin (rules only), SPY held and, on paper, the
        account: equity and return since the trial began (or `since`), before and after the
        estimated tax, and the book's lead over the twin and SPY."""
        return public.performance(_answer(deps, snapshot), since)

    @server.tool(annotations=READ)
    def decisions(last: int = 3) -> dict[str, Any]:
        """The monthly decision journal, newest first: the summary, the LLM's shift and
        satellite with its reasoning and expectations, the limits it hit, the month's result."""
        return public.decisions(_answer(deps, snapshot), last)

    @server.tool(annotations=READ)
    def exposure() -> dict[str, Any]:
        """Each decision's trend, volatility cap, halt, rule level, the LLM's shift and the
        enforced satellite weights."""
        return public.exposure(_answer(deps, snapshot))

    @server.tool(annotations=READ)
    def scorecard() -> dict[str, Any]:
        """The LLM's scorecard against the twin: returns after tax, tilt and shift scores, the
        tax realized, how often its expectations were right."""
        return public.scorecard(_answer(deps, snapshot))


def _operator_tools(server: MCPServer, deps: ServerDeps, op: Operator) -> None:
    def today() -> date:
        return deps.now().date()

    def read[T](query: Callable[[Any], T]) -> T:
        def run() -> T:
            with op.engine.connect() as conn:
                return query(conn)

        return _answer(deps, run)

    @server.tool(annotations=READ)
    def status() -> dict[str, Any]:
        """The system's state now: the kill switch, the drawdown halt and its peak, open
        mismatches, the next ranking (R) and execution (E) sessions, each job's last run."""
        return read(lambda c: operator.status(c, today=today(), environment=op.environment))

    @server.tool(annotations=READ)
    def jobs(job: str | None = None, status: str | None = None, limit: int = 10) -> dict[str, Any]:
        """Recent job runs, newest first, with their details and errors. Filter by `job`
        (e.g. post_market, ingest, allocate, portfolio_manager, execute_close) and `status`
        (SUCCEEDED, FAILED, RUNNING, SKIPPED_MARKET_CLOSED, SKIPPED_ALREADY_RAN,
        WAITING_PREDECESSOR, BLOCKED_PREDECESSOR)."""
        return read(lambda c: operator.jobs(c, job=job, status=status, limit=min(limit, 50)))

    @server.tool(annotations=READ)
    def logs(
        job: str, hours: float = 24.0, everything: bool = False, limit: int = 50
    ) -> dict[str, Any]:
        """A job's CloudWatch log events over the last `hours`: warnings, errors and Lambda's
        own failures (timeouts) by default, every line with `everything`."""
        if op.logs is None:
            raise ToolError("CloudWatch isn't configured for this server")
        if not JOB_NAME.match(job):
            raise ToolError(f"not a job name: {job!r}")
        reader = op.logs
        since = deps.now() - timedelta(hours=min(max(hours, 0.1), 24.0 * 14))
        events = _answer(
            deps, lambda: reader.events(job, since, everything=everything, limit=min(limit, 500))
        )
        return operator.record({"log_group": reader.group(job), "since": since, "events": events})

    @server.tool(annotations=READ)
    def portfolio() -> dict[str, Any]:
        """The broker's account now (equity, cash, positions), each leveraged position's resting
        stops against the plan's level (protected or not), other open orders, and the last
        close's holdings with the tax a sale of everything would owe."""
        broker = None if op.broker is None else _answer(deps, op.broker)
        return read(lambda c: operator.portfolio(c, op.risk, broker))

    @server.tool(annotations=READ)
    def trend() -> dict[str, Any]:
        """The latest trend reading: UP or DOWN against the 10-month average, the volatility cap,
        the halt, the rule level, and the facts gathered for the LLM (account, regime, macro)."""
        return read(operator.trend)

    @server.tool(annotations=READ)
    def tax(year: int | None = None) -> dict[str, Any]:
        """The tax picture: open lots per fund, realized gains by year, income, the tax a sale
        would owe at the last close, and the satellite's yearly tax budget used and left."""
        return read(lambda c: operator.tax(c, op.risk, year or today().year))

    @server.tool(annotations=READ)
    def decision(session: date | None = None) -> dict[str, Any]:
        """One decision whole (the latest, or the one executed on `session`): the facts the LLM
        saw, its answer, the guardrails' findings, the trades, the model and its cost."""
        return read(lambda c: operator.decision(c, session))


def _resources(server: MCPServer, deps: ServerDeps) -> None:
    def text(call: Callable[[], str]) -> str:
        try:
            return call()
        except docs.NotFoundError as exc:
            raise ResourceNotFoundError(str(exc)) from exc

    @server.resource("auratrade://readme", mime_type="text/markdown")
    def readme_index() -> str:
        """The README's sections: what the system does, every rule and threshold."""
        return text(lambda: docs.readme_index(deps.readme()))

    @server.resource("auratrade://readme/{section}", mime_type="text/markdown")
    def readme_section(section: str) -> str:
        """One README section by number, e.g. 5.4 (the Portfolio Manager) or 7.2 (the stop)."""
        return text(lambda: docs.readme_section(deps.readme(), section))

    @server.resource("auratrade://decisions", mime_type="text/markdown")
    def decision_index() -> str:
        """The decision log's entries: why things are the way they are."""
        return text(lambda: docs.decision_index(deps.readme()))

    @server.resource("auratrade://decision/{number}", mime_type="text/markdown")
    def decision_entry(number: str) -> str:
        """One decision-log entry by number, e.g. 163 (the neutral satellite and the tax rules)."""
        if not number.isdigit():
            raise ResourceNotFoundError(f"not a decision number: {number!r}")
        return text(lambda: docs.decision(deps.readme(), int(number)))

    @server.resource("auratrade://runbook", mime_type="text/markdown")
    def runbook() -> str:
        """What to do for each alert, and the trial's start checklist."""
        return text(lambda: docs.read(docs.RUNBOOK))

    @server.resource("auratrade://backtest", mime_type="text/markdown")
    def backtest() -> str:
        """The pre-registered backtest's report (1928-2026, after Israeli tax; the gate passed)."""
        return text(lambda: docs.read(docs.BACKTEST))


def _prompts(server: MCPServer, deps: ServerDeps) -> None:
    @server.prompt()
    def monthly_review(month: str = "") -> str:
        """The monthly journal review : judged on process, not returns."""
        which = f"the decision executed in {month}" if month else "the latest complete month"
        tools = "`performance`, `decisions`, `scorecard`, `exposure`"
        if deps.operator is not None:
            tools += ", `decision`, `tax`, `jobs`"
        return (
            f"Review {which} of the AuraTrade paper trial. Use {tools}.\n\n"
            "Judge it on process, not returns (README §15, #4): did the decision follow the "
            "rules and stay inside its limits (which limits clipped it, and why)? Were the "
            "LLM's expectations calibrated (direction and rough size against what happened)? "
            "How much tax did its changes realize, against the twin's, and was each change "
            "worth more than holding? Report the returns against the twin and SPY, before and "
            "after tax, but don't let one month's return decide the verdict.\n\n"
            "End with: what went well, what to watch next month, and anything that looks "
            "like a bug rather than a judgement call."
        )

    if deps.operator is None:
        return

    @server.prompt()
    def triage_alert(alert: str) -> str:
        """Triage a Telegram alert with the runbook, the status, the jobs and the logs."""
        return (
            "An AuraTrade alert arrived:\n\n"
            f"<alert>\n{alert}\n</alert>\n\n"
            "Read the runbook (resource auratrade://runbook) and find the row for this alert. "
            "Then check `status`, the affected job's recent runs with `jobs`, and its CloudWatch "
            "events with `logs`. Say what happened, whether money or protection is at risk "
            "(an unprotected leveraged position, a blocked sale), and the runbook's next step. "
            "This server is read-only: list any command the operator should run; don't claim "
            "to have run it."
        )

"""Run the read-only MCP server over stdio (README §5.10).

    uv run --env-file .env.paper python -m auratrade.mcp_server --snapshot <url or file>
    uv run python -m auratrade.mcp_server --public --snapshot <url or file>

`--public` serves the snapshot's tools alone and reads no setting or credential. Otherwise the
operator's tools read the database (a read-only session), the broker (its read calls) and
CloudWatch (the default AWS profile, e.g. `AWS_PROFILE=auratrade`). Logs go to stderr: stdout
is the protocol.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from auratrade.log import Redactor, configure_logging
from auratrade.mcp_server.server import Operator, ServerDeps, build_server
from auratrade.mcp_server.sources import BrokerReader, LogReader, Snapshot, read_only_engine

NY = ZoneInfo("America/New_York")


def _now() -> datetime:
    return datetime.now(NY)


def public_deps(snapshot: str | None, *, max_age: float = 0.0) -> ServerDeps:
    redactor = configure_logging(stream=sys.stderr)
    return ServerDeps(snapshot=Snapshot(snapshot, max_age=max_age), redact=redactor, now=_now)


def operator_deps(snapshot: str | None) -> ServerDeps:
    from auratrade.config import load_risk_config, load_settings

    settings = load_settings()
    redactor: Redactor = configure_logging(settings.secret_values(), stream=sys.stderr)
    if settings.database_url is None:
        raise SystemExit("DATABASE_URL is not set: run with --env-file, or --public")
    key, secret = settings.alpaca_api_key, settings.alpaca_secret_key

    def broker() -> BrokerReader:
        from auratrade.execution.alpaca_broker import AlpacaBroker

        if key is None or secret is None:
            from auratrade.mcp_server.sources import SourceError

            raise SourceError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
        return AlpacaBroker.paper(key.get_secret_value(), secret.get_secret_value())

    def logs_client() -> Any:
        import boto3

        return boto3.client("logs")

    operator = Operator(
        engine=read_only_engine(settings.database_url.get_secret_value()),
        risk=load_risk_config(settings.risk_config_path),
        broker=broker,
        logs=LogReader(
            logs_client,
            function_template=settings.job_function_template,
            environment=settings.environment,
        ),
        environment=settings.environment,
    )
    return ServerDeps(snapshot=Snapshot(snapshot), redact=redactor, now=_now, operator=operator)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="auratrade.mcp_server", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--public", action="store_true", help="the snapshot's tools only")
    parser.add_argument("--snapshot", help="the dashboard's snapshot: an https URL or a file")
    args = parser.parse_args(argv)
    deps = public_deps(args.snapshot) if args.public else operator_deps(args.snapshot)
    build_server(deps).run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())

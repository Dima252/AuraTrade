"""The remote MCP server's circuit breaker (README §5.10, #175).

The server's URL is public, capped at one concurrent run (10 requests a second at most). An
alarm on its invocations calls this when they look like a flood rather than a conversation: it
sets the server's reserved concurrency to 0, so every further request is throttled (Lambda
neither runs nor bills it), then alerts the operator. It stays off until re-armed by hand.

Outside `mcp_server/` on purpose: that package names no write (#174), and this is one.
"""

from __future__ import annotations

import os
from typing import Any

import structlog

from auratrade.log import configure_logging
from auratrade.notify.alerts import Alerter, LogAlerter

log = structlog.get_logger(__name__)

FUNCTION = "MCP_FUNCTION"
TOKEN, CHAT = "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"
PARAMETER_PATH = "ALERT_PARAMETER_PATH"
REARM = (
    "aws lambda put-function-concurrency --function-name {name} "
    "--reserved-concurrent-executions 1 --profile auratrade"
)


def trip(name: str, lambda_client: Any, alerter: Alerter, reason: str) -> None:
    # Off first: an alert that fails must not leave the server running.
    lambda_client.put_function_concurrency(FunctionName=name, ReservedConcurrentExecutions=0)
    log.warning("mcp_server_switched_off", function=name, reason=reason)
    alerter.alert(
        f"MCP server {name} switched off: {reason}. Nothing else is affected; it stays off "
        f"until re-armed: {REARM.format(name=name)}"
    )


def _alerter(ssm: Any, path: str | None) -> Alerter:
    """Telegram with the trading stack's own bot (the two parameters alone), else the logs."""
    if not path:
        return LogAlerter()
    found = ssm.get_parameters(Names=[path + TOKEN, path + CHAT], WithDecryption=True)
    values = {p["Name"].rsplit("/", 1)[-1]: p["Value"] for p in found.get("Parameters", [])}
    if TOKEN not in values or CHAT not in values:
        return LogAlerter()
    from auratrade.notify.telegram import TelegramNotifier

    return TelegramNotifier(values[TOKEN], values[CHAT])


def handler(event: dict[str, Any], context: object) -> None:
    """A CloudWatch alarm's Lambda action."""
    import boto3

    configure_logging()
    state = event.get("alarmData", {}).get("state", {})
    if state.get("value") != "ALARM":
        return
    alerter = _alerter(boto3.client("ssm"), os.environ.get(PARAMETER_PATH))
    reason = state.get("reason") or "its invocations crossed the alarm's threshold"
    trip(os.environ[FUNCTION], boto3.client("lambda"), alerter, reason)

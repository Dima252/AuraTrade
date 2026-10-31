"""The public tools as a remote MCP server: a Lambda function with a public URL, for any MCP
client (README §5.10, #38). `infra/stacks/mcp.py` bounds what it can cost.

MCP's Streamable HTTP in its stateless, JSON-response form: each POST is one JSON-RPC exchange
answered in full, so nothing lives between invocations and no session needs a sticky route.
Each invocation runs the SDK's own ASGI app in-process on the function URL's request. It serves
`--public` alone: the snapshot's tools, the documents and the review prompt; it reads no setting
or credential, and the function's role can read the snapshot's object and nothing else.
"""

from __future__ import annotations

import asyncio
import base64
import os
from functools import cache
from typing import Any

import httpx
from mcp.server.transport_security import TransportSecuritySettings

from auratrade.mcp_server.__main__ import public_deps
from auratrade.mcp_server.server import ServerDeps, build_server

SNAPSHOT = "MCP_SNAPSHOT"
SNAPSHOT_MAX_AGE = 60.0
PATH = "/mcp"
# The Host is the function URL's own; DNS rebinding guards a server on a laptop's localhost, not
# this one.
ANY_HOST = TransportSecuritySettings(enable_dns_rebinding_protection=False)
# Set again by httpx for the request it sends to the app.
HOP_HEADERS = frozenset({"host", "content-length", "transfer-encoding", "connection"})


@cache
def _deps() -> ServerDeps:
    # The snapshot changes once an evening: one S3 read a minute per container, however busy.
    return public_deps(os.environ.get(SNAPSHOT), max_age=SNAPSHOT_MAX_AGE)


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    return asyncio.run(respond(event, _deps()))


async def respond(event: dict[str, Any], deps: ServerDeps) -> dict[str, Any]:
    """One function URL request (payload format 2.0) through a fresh server: the SDK's session
    manager runs once per instance, and building one costs nothing next to the request."""
    method = event["requestContext"]["http"]["method"]
    if method != "POST":
        # The SDK would hold a GET open as the server's stream until the function times out.
        return {
            "statusCode": 405,
            "headers": {"allow": "POST"},
            "body": "",
            "isBase64Encoded": False,
        }
    server = build_server(deps)
    app = server.streamable_http_app(
        streamable_http_path=PATH,
        json_response=True,
        stateless_http=True,
        transport_security=ANY_HOST,
    )
    body = event.get("body") or ""
    content = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    query = event.get("rawQueryString") or ""
    headers = {
        k: v for k, v in (event.get("headers") or {}).items() if k.lower() not in HOP_HEADERS
    }
    async with server.session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://mcp.local") as client:
            response = await client.post(
                event.get("rawPath") or "/",
                params=query or None,
                headers=headers,
                content=content,
            )
    return {
        "statusCode": response.status_code,
        "headers": {k: v for k, v in response.headers.items() if k.lower() not in HOP_HEADERS},
        "body": response.text,
        "isBase64Encoded": False,
    }

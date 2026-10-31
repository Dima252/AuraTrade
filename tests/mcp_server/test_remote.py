"""The remote server's Lambda handler: function URL requests through the SDK's own HTTP app
(README §5.10)."""

import base64
import json
from typing import Any

import pytest

from auratrade.mcp_server import remote
from auratrade.mcp_server.server import ServerDeps
from tests.mcp_server.test_server import PUBLIC_TOOLS, _public

ACCEPT = "application/json, text/event-stream"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _event(
    message: object, *, method: str = "POST", path: str = "/mcp", encode: bool = False
) -> dict[str, Any]:
    body = json.dumps(message)
    return {
        "version": "2.0",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {
            # As the function URL hands them over: its own host, the client's length.
            "host": "abc.lambda-url.us-east-1.on.aws",
            "content-length": str(len(body)),
            "content-type": "application/json",
            "accept": ACCEPT,
            "origin": "https://claude.ai",
        },
        "requestContext": {"http": {"method": method, "path": path}},
        "body": base64.b64encode(body.encode()).decode() if encode else body,
        "isBase64Encoded": encode,
    }


def _call(method: str, params: dict[str, Any] | None = None, *, id: int = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}


async def _answer(event: dict[str, Any], deps: ServerDeps | None = None) -> dict[str, Any]:
    response = await remote.respond(event, deps or _public())
    assert response["statusCode"] == 200, response
    assert response["headers"]["content-type"].startswith("application/json")
    reply: dict[str, Any] = json.loads(response["body"])
    assert "error" not in reply, reply
    result: dict[str, Any] = reply["result"]
    return result


@pytest.mark.anyio
async def test_initialize() -> None:
    result = await _answer(
        _event(
            _call(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            )
        )
    )

    assert result["serverInfo"]["name"] == "auratrade"
    assert "read-only" in result["instructions"]


@pytest.mark.anyio
async def test_the_public_tools_alone() -> None:
    result = await _answer(_event(_call("tools/list")))

    assert {tool["name"] for tool in result["tools"]} == PUBLIC_TOOLS
    assert all(tool["annotations"]["readOnlyHint"] for tool in result["tools"])
    # Released protocol versions carry no `extensions` capability: hosts find the page through
    # the tool's own `_meta`.
    (dashboard,) = (tool for tool in result["tools"] if tool["name"] == "dashboard")
    assert dashboard["_meta"] == {"ui": {"resourceUri": "ui://auratrade/dashboard.html"}}


@pytest.mark.anyio
async def test_a_tool_call_in_a_base64_body() -> None:
    event = _event(_call("tools/call", {"name": "performance", "arguments": {}}), encode=True)

    result = await _answer(event)

    assert not result["isError"]
    assert result["structuredContent"]["mode"] == "paper"
    assert result["structuredContent"]["returns"]["book"]["return"] is not None


@pytest.mark.anyio
async def test_a_resource() -> None:
    result = await _answer(_event(_call("resources/read", {"uri": "auratrade://decision/37"})))

    assert "Read-only." in result["contents"][0]["text"]


@pytest.mark.anyio
async def test_the_dashboard_and_its_page() -> None:
    call = await _answer(_event(_call("tools/call", {"name": "dashboard", "arguments": {}})))
    page = await _answer(_event(_call("resources/read", {"uri": "ui://auratrade/dashboard.html"})))

    assert not call["isError"]
    assert call["structuredContent"]["series"]
    assert "AuraTrade through 2026-12-04" in call["content"][0]["text"]
    assert page["contents"][0]["mimeType"] == "text/html;profile=mcp-app"


@pytest.mark.anyio
async def test_no_snapshot_yet() -> None:
    event = _event(_call("tools/call", {"name": "scorecard", "arguments": {}}))

    result = await _answer(event, _public({}))

    assert not result["isError"]
    assert result["structuredContent"]["scorecard"] == {}


@pytest.mark.anyio
async def test_each_request_gets_a_fresh_server() -> None:
    # The SDK's session manager runs once per instance; a warm Lambda serves many requests.
    deps = _public()
    for n in range(3):
        result = await _answer(_event(_call("tools/list", id=n)), deps)
        assert len(result["tools"]) == len(PUBLIC_TOOLS)


@pytest.mark.anyio
async def test_only_the_mcp_path() -> None:
    response = await remote.respond(_event(_call("tools/list"), path="/other"), _public())

    assert response["statusCode"] == 404


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_post_alone(method: str) -> None:
    # Stateless and JSON-only: no server stream to hold open, no session to end.
    event = _event(None, method=method)
    event["body"] = None

    response = await remote.respond(event, _public())

    assert response["statusCode"] == 405
    assert response["headers"]["allow"] == "POST"


def test_the_handler_reads_its_snapshot_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(remote.SNAPSHOT, "s3://site-bucket/data/snapshot.json")
    remote._deps.cache_clear()
    try:
        deps = remote._deps()
        assert deps.operator is None
        assert deps.snapshot._location == "s3://site-bucket/data/snapshot.json"
        assert deps.snapshot._max_age == remote.SNAPSHOT_MAX_AGE
        assert remote._deps() is deps
        response = remote.handler(_event(_call("tools/list")), None)
        assert response["statusCode"] == 200
    finally:
        remote._deps.cache_clear()

"""A scripted vendor server for `httpx.MockTransport`, and the recorded fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> Any:
    return json.loads(fixture_text(name))


def ok(body: Any) -> httpx.Response:
    if isinstance(body, str):
        return httpx.Response(200, text=body)
    return httpx.Response(200, json=body)


class Server:
    """Answers each request with the next scripted response (or raises it)."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def params(self, i: int = 0) -> dict[str, str]:
        return dict(self.requests[i].url.params)

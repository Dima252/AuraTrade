"""PAID: record real Claude responses as test fixtures (task 4.1; CLAUDE.md §3.3).

    uv run python scripts/record_claude_fixtures.py --paid

One small decision through `auratrade.llm.client`: a tool round, then a structured answer, at
the Portfolio Manager's model and effort. Estimated cost under $0.10 (a ~400-token prompt, two
turns of adaptive thinking at effort `high`). Run it only with the user's OK. The response
bodies land in `tests/fixtures/anthropic/recorded_*.json`; a body holding the key is refused.
The facts are made up: the call exercises the API's shapes, not a real decision.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import anthropic
import httpx2
from pydantic import BaseModel, Field

from auratrade.config import Settings, load_risk_config
from auratrade.llm.client import ClaudeClient, Tool

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "anthropic"

SYSTEM = """You are testing AuraTrade's decision format. Read the facts, call get_etf_facts \
once for GLD, then answer in the required format. The facts are made up."""

FACTS = {
    "trend": {"state": "UP", "rule_level": 1.3, "sma_distance": 0.06, "volatility": 0.14},
    "account": {"equity": 100000, "drawdown": 0.03},
    "satellite_list": ["GLD", "IEF", "BIL"],
}


class Weight(BaseModel):
    symbol: str
    weight: float = Field(description="Fraction of equity")


class Answer(BaseModel):
    core_shift: float = Field(description="-0.2 to +0.2")
    satellite: list[Weight]
    summary: str


class Symbol(BaseModel):
    symbol: str


class Recorder(httpx2.HTTPTransport):
    def __init__(self) -> None:
        super().__init__()
        self.bodies: list[dict[str, Any]] = []

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        response = super().handle_request(request)
        response.read()
        self.bodies.append(response.json())
        return response


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--paid", action="store_true", help="confirm the paid call")
    if not parser.parse_args().paid:
        sys.exit("refused: this makes paid Claude calls; pass --paid with the user's OK")

    settings = Settings()
    if settings.anthropic_api_key is None:
        sys.exit("ANTHROPIC_API_KEY is not set")
    key = settings.anthropic_api_key.get_secret_value()
    config = load_risk_config().llm
    recorder = Recorder()
    sdk = anthropic.Anthropic(
        api_key=key,
        max_retries=config.max_retries,
        timeout=config.request_timeout_seconds,
        http_client=anthropic.DefaultHttpxClient(transport=recorder),
    )
    client = ClaudeClient(sdk, config, settings.pm_model, settings.pm_effort)
    tool = Tool(
        "get_etf_facts",
        "One satellite ETF's facts: returns, volatility, distance from its 200-session SMA.",
        Symbol,
        lambda args: json.dumps({"symbol": args.symbol, "return_12m": 0.18, "vol": 0.15}),
    )
    outcome = client.decide(system=SYSTEM, content=json.dumps(FACTS), answer=Answer, tools=[tool])

    for n, body in enumerate(recorder.bodies, 1):
        text = json.dumps(body, indent=2)
        if key in text:
            sys.exit("refused: a response body holds the key")
        (OUT / f"recorded_turn{n}.json").write_text(text + "\n", encoding="utf-8")
    print(
        f"{outcome.failure or 'ok'}: {len(outcome.turns)} turns, ${outcome.cost_usd:.4f}, "
        f"model {outcome.model}, {len(recorder.bodies)} bodies written to {OUT}"
    )


if __name__ == "__main__":
    main()

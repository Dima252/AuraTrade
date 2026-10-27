"""The Claude client on hand-built responses in the API's wire format (tests/fixtures/anthropic):
no request leaves the machine (CLAUDE.md §3.3)."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pydantic
import pytest

from auratrade.config import Effort, LlmConfig, Settings, load_risk_config
from auratrade.llm.client import FALLBACK_BETA, ClaudeClient, Failure, Tool

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "anthropic"
CONFIG = load_risk_config().llm
NOON = datetime(2026, 11, 2, 17, 0, tzinfo=UTC)


class Answer(pydantic.BaseModel):
    core_shift: float
    summary: str


class Symbol(pydantic.BaseModel):
    symbol: str


def fixture(name: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return body


def answer_with(text: str, stop_reason: str = "end_turn") -> dict[str, Any]:
    body = fixture("answer")
    body["content"][1]["text"] = text
    body["stop_reason"] = stop_reason
    return body


class Api:
    """Serves the queued responses in order and keeps every request body."""

    def __init__(self, *responses: dict[str, Any] | tuple[int, dict[str, Any]] | Exception):
        self.queue = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.headers: list[httpx2.Headers] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        self.headers.append(request.headers)
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body = item if isinstance(item, tuple) else (200, item)
        return httpx2.Response(status, json=body, headers={"request-id": f"req_{len(self.queue)}"})


def client(
    api: Api,
    config: LlmConfig = CONFIG,
    now: Callable[[], datetime] = lambda: NOON,
) -> ClaudeClient:
    sdk = anthropic.Anthropic(
        api_key="sk-ant-test",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(api)),
    )
    return ClaudeClient(sdk, config, "claude-opus-5-5", Effort.HIGH, now=now)


def facts_tool(calls: list[str] | None = None) -> Tool[Symbol]:
    def run(args: Symbol) -> str:
        if calls is not None:
            calls.append(args.symbol)
        return f'<etf_facts symbol="{args.symbol}">1y +12%</etf_facts>'

    return Tool("get_etf_facts", "One satellite ETF's facts.", Symbol, run)


def decide(c: ClaudeClient, **kwargs: Any) -> Any:
    return c.decide(
        system="You are the Portfolio Manager.", content="facts", answer=Answer, **kwargs
    )


# --- the request ------------------------------------------------------------------------------


def test_the_request_carries_the_readme_settings() -> None:
    api = Api(fixture("answer"))
    outcome = decide(client(api), tools=[facts_tool()])
    assert outcome.ok
    body = api.requests[0]
    assert body["model"] == "claude-opus-5-5"
    assert body["max_tokens"] == CONFIG.max_tokens
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"]["effort"] == "high"
    schema = body["output_config"]["format"]["schema"]
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"core_shift", "summary"}
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["fallbacks"] == "default"
    assert FALLBACK_BETA in api.headers[0]["anthropic-beta"]
    assert body["tool_choice"] == {"type": "auto"}
    (tool,) = body["tools"]
    assert tool["strict"] is True
    assert tool["input_schema"]["additionalProperties"] is False
    assert body["messages"] == [{"role": "user", "content": "facts"}]


def test_without_tools_or_fallbacks_the_request_names_neither() -> None:
    api = Api(fixture("answer"))
    config = CONFIG.model_copy(update={"fallbacks": False})
    assert decide(client(api, config)).ok
    body = api.requests[0]
    assert {"tools", "tool_choice", "fallbacks"}.isdisjoint(body)
    assert "anthropic-beta" not in api.headers[0]


# --- a decision with a tool round ------------------------------------------------------------


def test_a_tool_round_then_a_validated_answer() -> None:
    api = Api(fixture("tool_use"), fixture("answer"))
    calls: list[str] = []
    outcome = decide(client(api), tools=[facts_tool(calls)])

    assert outcome.ok
    assert outcome.failure is None
    assert outcome.detail == ""
    assert outcome.value == Answer(
        core_shift=-0.1, summary="Trim the core while volatility is high."
    )
    assert calls == ["GLD"]
    second = api.requests[1]["messages"]
    # The assistant turn goes back unchanged, thinking block included (preserved thinking).
    assert [b["type"] for b in second[1]["content"]] == ["thinking", "tool_use"]
    assert second[1]["content"][0]["signature"] == "EqQBCkYIBxgCKkDsig"
    assert second[2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_01Facts",
                "content": '<etf_facts symbol="GLD">1y +12%</etf_facts>',
            }
        ],
    }
    first, last = outcome.turns
    assert (first.tools, last.tools) == (("get_etf_facts",), ())
    assert first.thinking_tokens == 733
    assert (first.cache_write_tokens, last.cache_read_tokens) == (1668, 1668)
    # The 0.3g spike's two turns at Opus 5.5's prices: $0.0410 and $0.0632.
    assert first.cost_usd == pytest.approx((3355 * 4 + 1668 * 5 + 960 * 20) / 1e6)
    assert last.cost_usd == pytest.approx((6648 * 4 + 1668 * 0.2 + 1815 * 20) / 1e6)
    assert outcome.cost_usd == pytest.approx(0.1042, abs=1e-4)
    assert not outcome.cost_alert
    assert not outcome.fallback
    assert outcome.model == "claude-opus-5-5"
    assert first.request_id is not None


def test_a_tool_error_goes_back_to_the_model() -> None:
    def broken(args: Symbol) -> str:
        raise RuntimeError("vendor down")

    api = Api(fixture("tool_use"), fixture("answer"))
    outcome = decide(client(api), tools=[Tool("get_etf_facts", "facts", Symbol, broken)])
    assert outcome.ok
    (result,) = api.requests[1]["messages"][2]["content"]
    assert result["is_error"] is True
    assert result["content"] == "the tool failed: RuntimeError"


def test_invalid_tool_arguments_and_an_unknown_tool_are_errors() -> None:
    first = fixture("tool_use")
    first["content"][1]["input"] = {"ticker": "GLD"}
    first["content"].append(
        {"type": "tool_use", "id": "toolu_01Unknown", "name": "get_price", "input": {}}
    )
    api = Api(first, fixture("answer"))
    assert decide(client(api), tools=[facts_tool()]).ok
    invalid, unknown = api.requests[1]["messages"][2]["content"]
    assert invalid["is_error"]
    assert invalid["content"].startswith("invalid arguments:")
    assert unknown == {
        "type": "tool_result",
        "tool_use_id": "toolu_01Unknown",
        "content": "unknown tool get_price",
        "is_error": True,
    }


def test_a_tool_may_return_content_blocks() -> None:
    image = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x"}}
    ]
    api = Api(fixture("tool_use"), fixture("answer"))
    tool = Tool("get_etf_facts", "chart", Symbol, lambda args: image)
    assert decide(client(api), tools=[tool]).ok
    assert api.requests[1]["messages"][2]["content"][0]["content"] == image


def test_the_last_turn_forbids_tools() -> None:
    config = CONFIG.model_copy(update={"max_turns": 2})
    api = Api(fixture("tool_use"), fixture("answer"))
    assert decide(client(api, config), tools=[facts_tool()]).ok
    assert [r["tool_choice"] for r in api.requests] == [{"type": "auto"}, {"type": "none"}]


def test_still_calling_tools_on_the_last_turn_fails() -> None:
    config = CONFIG.model_copy(update={"max_turns": 2})
    api = Api(fixture("tool_use"), fixture("tool_use"))
    outcome = decide(client(api, config), tools=[facts_tool()])
    assert outcome.failure is Failure.TOO_MANY_TURNS
    assert outcome.value is None
    assert len(outcome.turns) == 2


def test_tool_names_must_be_unique() -> None:
    with pytest.raises(ValueError, match="unique"):
        decide(client(Api()), tools=[facts_tool(), facts_tool()])


# --- fallbacks and refusals -------------------------------------------------------------------


def test_a_fallback_served_answer_is_recorded_and_priced_per_hop() -> None:
    outcome = decide(client(Api(fixture("fallback_answer"))))
    assert outcome.ok
    assert outcome.value == Answer(core_shift=0.0, summary="Hold the rule level.")
    assert outcome.fallback
    assert outcome.model == "claude-opus-4-8"
    (turn,) = outcome.turns
    assert turn.cost_usd == pytest.approx((4000 * 4 + 100 * 20 + 4000 * 5 + 400 * 25) / 1e6)


def test_a_mid_output_fallback_echoes_only_the_text_before_its_boundary() -> None:
    calls: list[str] = []
    api = Api(fixture("fallback_tool_use"), fixture("answer"))
    assert decide(client(api), tools=[facts_tool(calls)]).ok
    assert calls == ["GLD"]  # the declined hop's TLT call is not run
    echoed = api.requests[1]["messages"][1]["content"]
    assert [(b["type"], b.get("id")) for b in echoed] == [
        ("text", None),
        ("tool_use", "toolu_01Served"),
    ]


def test_a_refusal_after_the_chain_fails_closed() -> None:
    outcome = decide(client(Api(fixture("refusal"))))
    assert outcome.failure is Failure.REFUSAL
    assert outcome.refusal_category == "general_harms"
    assert outcome.detail == "declined (general_harms)"
    assert outcome.value is None
    assert not outcome.fallback  # no hop served it
    # Whatever usage the API reports is priced (whether a refusal is billed depends on it).
    assert outcome.cost_usd == pytest.approx((2400 * 4 + 2400 * 5) / 1e6)


def test_a_refusal_without_details() -> None:
    body = fixture("refusal")
    body["stop_details"] = None
    outcome = decide(client(Api(body)))
    assert outcome.failure is Failure.REFUSAL
    assert outcome.refusal_category is None
    assert outcome.detail == "declined (no category)"


def test_an_unlisted_model_is_priced_at_the_dearest() -> None:
    body = fixture("answer")
    body["model"] = "claude-opus-6"
    (turn,) = decide(client(Api(body))).turns
    assert turn.cost_usd == pytest.approx((6648 * 5 + 1668 * 0.5 + 1815 * 25) / 1e6)


# --- answers that fail ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "detail"),
    [("", "the answer has no text"), ('{"core_shift": "lots"}', "validation error")],
)
def test_an_invalid_answer_fails(text: str, detail: str) -> None:
    outcome = decide(client(Api(answer_with(text))))
    assert outcome.failure is Failure.INVALID_OUTPUT
    assert detail in outcome.detail


def test_a_long_validation_error_is_clipped() -> None:
    class Strict(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(extra="forbid")
        x: int

    extra = json.dumps({f"field_{i}": i for i in range(200)} | {"x": 1})
    c = client(Api(answer_with(extra)))
    outcome = c.decide(system="s", content="c", answer=Strict)
    assert outcome.failure is Failure.INVALID_OUTPUT
    assert len(outcome.detail) == 500
    assert outcome.detail.endswith("…")


@pytest.mark.parametrize(
    ("stop_reason", "failure"),
    [("max_tokens", Failure.TRUNCATED), ("pause_turn", Failure.UNEXPECTED_STOP)],
)
def test_other_stop_reasons_fail(stop_reason: str, failure: Failure) -> None:
    outcome = decide(client(Api(answer_with("{}", stop_reason))))
    assert outcome.failure is failure
    assert len(outcome.turns) == 1


def test_an_expensive_decision_alerts() -> None:
    config = CONFIG.model_copy(update={"cost_alert_usd": 0.05})
    outcome = decide(client(Api(fixture("answer")), config))
    assert outcome.ok
    assert outcome.cost_alert


# --- API errors and the deadline -------------------------------------------------------------


def test_an_api_error_fails_closed() -> None:
    outcome = decide(client(Api((529, fixture("overloaded")))))
    assert outcome.failure is Failure.API_ERROR
    assert outcome.detail.startswith("HTTP 529")
    assert outcome.turns == ()


@pytest.mark.parametrize(
    ("error", "failure"),
    [
        (httpx2.ConnectError("refused"), Failure.API_ERROR),
        (httpx2.ReadTimeout("slow"), Failure.TIMEOUT),
    ],
)
def test_transport_errors_fail_closed(error: Exception, failure: Failure) -> None:
    assert decide(client(Api(error))).failure is failure


def test_no_request_starts_after_the_deadline() -> None:
    api = Api(fixture("answer"))
    outcome = decide(client(api), deadline=NOON)
    assert outcome.failure is Failure.DEADLINE
    assert api.requests == []


def test_a_late_answer_is_discarded() -> None:
    times: Iterator[datetime] = iter([NOON, NOON + timedelta(minutes=10)])
    outcome = decide(
        client(Api(fixture("answer")), now=lambda: next(times)),
        deadline=NOON + timedelta(minutes=5),
    )
    assert outcome.failure is Failure.DEADLINE
    assert outcome.detail == "the answer arrived after the deadline"
    assert len(outcome.turns) == 1  # the late turn is still paid for and reported


def test_an_answer_in_time_is_kept() -> None:
    outcome = decide(client(Api(fixture("answer"))), deadline=NOON + timedelta(minutes=5))
    assert outcome.ok


# --- the recorded call (scripts/record_claude_fixtures.py, 2026-10-27) ------------------------


class Weight(pydantic.BaseModel):
    symbol: str
    weight: float


class RecordedAnswer(pydantic.BaseModel):
    core_shift: float
    satellite: list[Weight]
    summary: str


def test_the_recorded_call_replays_to_a_validated_answer() -> None:
    api = Api(fixture("recorded_turn1"), fixture("recorded_turn2"))
    calls: list[str] = []
    outcome = client(api).decide(
        system="s", content="facts", answer=RecordedAnswer, tools=[facts_tool(calls)]
    )
    assert outcome.ok
    assert outcome.value is not None
    assert outcome.value.core_shift == 0.1
    assert outcome.value.satellite == [Weight(symbol="GLD", weight=0.1)]
    assert calls == ["GLD"]
    # The real tool_use block carries `caller`; it goes back as received.
    echoed = api.requests[1]["messages"][1]["content"]
    assert echoed[0]["caller"] == {"type": "direct"}
    first, last = outcome.turns
    # `usage.iterations` comes without a fallback too, its model null: the requested model.
    assert not outcome.fallback
    assert (first.cache_write_tokens, last.cache_read_tokens) == (1049, 1049)
    assert last.thinking_tokens == 214
    assert first.cost_usd == pytest.approx((4 * 4 + 1049 * 5 + 55 * 20) / 1e6)
    assert last.cost_usd == pytest.approx((2 * 4 + 1049 * 0.2 + 93 * 5 + 501 * 20) / 1e6)
    assert outcome.cost_usd == pytest.approx(0.0171, abs=1e-4)


# --- construction -----------------------------------------------------------------------------


def test_the_model_needs_a_price() -> None:
    sdk = anthropic.Anthropic(api_key="sk-ant-test")
    with pytest.raises(ValueError, match="no price"):
        ClaudeClient(sdk, CONFIG, "claude-haiku-5-5", Effort.LOW)


def test_from_settings_needs_the_key() -> None:
    settings = Settings(_env_file=None, anthropic_api_key=None)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        ClaudeClient.from_settings(settings, CONFIG, model="claude-opus-5-5", effort=Effort.HIGH)


def test_from_settings_builds_the_sdk_client() -> None:
    settings = Settings(_env_file=None, anthropic_api_key="sk-ant-test")  # type: ignore[call-arg]
    c = ClaudeClient.from_settings(
        settings, CONFIG, model=settings.pm_model, effort=settings.pm_effort
    )
    assert (c.model, c.effort) == ("claude-opus-5-5", Effort.HIGH)
    assert c.api.max_retries == CONFIG.max_retries
    assert c.api.timeout == CONFIG.request_timeout_seconds

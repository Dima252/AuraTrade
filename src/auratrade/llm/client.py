"""The Claude client (README §9; task 4.1): one decision is one conversation, run to a
validated answer or to a named failure.

- **The model always thinks** (`claude-opus-5-5` rejects disabled thinking); the depth is the
  explicit `effort`, since the model's own default (`medium`) is not the README's.
- **Structured output:** the answer's Pydantic model becomes `output_config.format`, and the
  final text is validated against the same model.
- **Tools** are strict and `tool_choice` is `auto` (a forced choice is a 400 on this model).
  Each tool's arguments are validated before it runs; a tool that fails returns an error
  result to the model, which decides without it. The last allowed turn sends
  `tool_choice: none`, so the model must answer.
- **Caching:** a breakpoint on the system prompt (with the tools before it) and the top-level
  automatic breakpoint, so each tool round reads the previous one from the cache.
- **Refusals:** server-side fallbacks (`fallbacks: "default"`) re-run a declined request on
  the model Anthropic routes that category to; the answering model is recorded on every turn.
  A refusal that survives the chain is a failure.
- **Fail closed** (CLAUDE.md §3.6): an API problem never raises. The caller gets a failure and
  holds (#164). No request starts after the deadline, each attempt is cut to it,
  and an answer that arrives after it is discarded.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast

import anthropic
import pydantic
import structlog
from anthropic.lib._parse._transform import transform_schema
from anthropic.types.beta import (
    BetaContentBlock,
    BetaContentBlockParam,
    BetaMessage,
    BetaMessageParam,
    BetaToolParam,
)

from auratrade.config import Effort, LlmConfig, Settings, TokenPrices

log = structlog.get_logger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# How much of a validation error or an API message a failure keeps (they go to logs and alerts).
DETAIL_CHARS = 500

# A tool returns text, or content blocks (an image with its caption, for a chart).
ToolOutput = str | list[dict[str, Any]]


class Failure(StrEnum):
    API_ERROR = "API_ERROR"  # a status error or a lost connection, after the SDK's retries
    TIMEOUT = "TIMEOUT"  # one attempt ran out of time
    DEADLINE = "DEADLINE"  # the job's deadline passed: no request started, or a late answer
    REFUSAL = "REFUSAL"  # declined, fallbacks included
    TRUNCATED = "TRUNCATED"  # hit max_tokens before answering
    INVALID_OUTPUT = "INVALID_OUTPUT"  # no text, or text that fails the answer's schema
    TOO_MANY_TURNS = "TOO_MANY_TURNS"  # still calling tools on the last allowed turn
    UNEXPECTED_STOP = "UNEXPECTED_STOP"  # any other stop reason


@dataclass(frozen=True)
class Tool[A: pydantic.BaseModel]:
    """A strict tool: its arguments are a Pydantic model; ``run`` gets them validated."""

    name: str
    description: str
    arguments: type[A]
    run: Callable[[A], ToolOutput]

    def param(self) -> BetaToolParam:
        return {
            "name": self.name,
            "description": self.description,
            "strict": True,
            "input_schema": cast(Any, transform_schema(self.arguments)),
        }

    def call(self, raw: object) -> tuple[ToolOutput, bool]:
        """The tool's output and whether it is an error, never an exception."""
        try:
            args = self.arguments.model_validate(raw)
        except pydantic.ValidationError as e:
            return f"invalid arguments: {_clip(str(e))}", True
        try:
            return self.run(args), False
        except Exception as e:  # a tool's failure is reported to the model, not the job
            log.warning("llm.tool_failed", tool=self.name, error=e)
            return f"the tool failed: {type(e).__name__}", True


@dataclass(frozen=True)
class Turn:
    """One request: who answered, how it stopped, what it used and cost."""

    model: str
    stop_reason: str | None
    seconds: float
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    thinking_tokens: int | None
    tools: tuple[str, ...]
    cost_usd: float
    fallback: bool  # a fallback model served this turn
    request_id: str | None


@dataclass(frozen=True)
class Outcome[T: pydantic.BaseModel]:
    """A validated answer, or the failure that stopped the decision (never both)."""

    value: T | None
    failure: Failure | None
    detail: str
    turns: tuple[Turn, ...]
    cost_alert: bool
    refusal_category: str | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None

    @property
    def cost_usd(self) -> float:
        return sum(t.cost_usd for t in self.turns)

    @property
    def model(self) -> str | None:
        """The model that gave the last answer (README §9: stored on every decision)."""
        return self.turns[-1].model if self.turns else None

    @property
    def fallback(self) -> bool:
        return any(t.fallback for t in self.turns)


@dataclass
class ClaudeClient:
    """Runs decisions on one model at one effort. Build it with ``from_settings``."""

    api: anthropic.Anthropic
    config: LlmConfig
    model: str
    effort: Effort
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    monotonic: Callable[[], float] = field(default=time.monotonic)

    def __post_init__(self) -> None:
        if self.model not in self.config.prices:
            raise ValueError(f"risk.yaml llm.prices has no price for {self.model}")

    @classmethod
    def from_settings(
        cls, settings: Settings, config: LlmConfig, *, model: str, effort: Effort
    ) -> ClaudeClient:
        if settings.anthropic_api_key is None:
            raise ValueError("ANTHROPIC_API_KEY is not set")
        api = anthropic.Anthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            max_retries=config.max_retries,
            timeout=config.request_timeout_seconds,
        )
        return cls(api=api, config=config, model=model, effort=effort)

    def decide[T: pydantic.BaseModel](
        self,
        *,
        system: str,
        content: str | list[dict[str, Any]],
        answer: type[T],
        tools: Sequence[Tool[Any]] = (),
        deadline: datetime | None = None,
    ) -> Outcome[T]:
        """Runs the conversation: tool rounds, then one answer validated against ``answer``."""
        by_name = {t.name: t for t in tools}
        if len(by_name) != len(tools):
            raise ValueError("tool names must be unique")
        messages: list[BetaMessageParam] = [{"role": "user", "content": cast(Any, content)}]
        turns: list[Turn] = []

        def fail(failure: Failure, detail: str, category: str | None = None) -> Outcome[T]:
            outcome: Outcome[T] = Outcome(
                None, failure, detail, tuple(turns), self._alert(turns), category
            )
            log.warning("llm.failed", failure=failure, detail=detail, **_summary(outcome))
            return outcome

        for n in range(self.config.max_turns):
            timeout = self.config.request_timeout_seconds
            if deadline is not None:
                left = (deadline - self.now()).total_seconds()
                if left <= 0:
                    return fail(Failure.DEADLINE, "the deadline passed before the request")
                timeout = min(timeout, left)
            last = n == self.config.max_turns - 1
            started = self.monotonic()
            try:
                response = self._create(system, messages, answer, tools, timeout, last)
            except anthropic.APITimeoutError:
                return fail(Failure.TIMEOUT, f"no response within {timeout:.0f} s")
            except anthropic.APIConnectionError as e:
                return fail(Failure.API_ERROR, f"connection: {type(e).__name__}")
            except anthropic.APIStatusError as e:
                return fail(Failure.API_ERROR, f"HTTP {e.status_code}: {_clip(e.message)}")
            turns.append(self._turn(response, self.monotonic() - started))
            if deadline is not None and self.now() > deadline:
                return fail(Failure.DEADLINE, "the answer arrived after the deadline")

            match response.stop_reason:
                case "end_turn":
                    return self._answer(response, answer, turns, fail)
                case "tool_use":
                    kept = _echoed(response.content)
                    echo = [cast(BetaContentBlockParam, b.to_dict()) for b in kept]
                    messages.append({"role": "assistant", "content": echo})
                    messages.append({"role": "user", "content": _run_tools(kept, by_name)})
                case "refusal":
                    details = response.stop_details
                    category = details.category if details else None
                    return fail(
                        Failure.REFUSAL, f"declined ({category or 'no category'})", category
                    )
                case "max_tokens":
                    return fail(
                        Failure.TRUNCATED, f"stopped at max_tokens {self.config.max_tokens}"
                    )
                case other:
                    return fail(Failure.UNEXPECTED_STOP, f"stop reason {other}")
        return fail(
            Failure.TOO_MANY_TURNS, f"still calling tools after {self.config.max_turns} turns"
        )

    def _create(
        self,
        system: str,
        messages: list[BetaMessageParam],
        answer: type[pydantic.BaseModel],
        tools: Sequence[Tool[Any]],
        timeout: float,
        last: bool,
    ) -> BetaMessage:
        extra: dict[str, Any] = {}
        if self.config.fallbacks:
            extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"}
        if tools:
            extra["tools"] = [t.param() for t in tools]
            extra["tool_choice"] = {"type": "none" if last else "auto"}
        # The optional parameters go in as **extra, so the overload is pinned by annotation.
        response: BetaMessage = self.api.with_options(timeout=timeout).beta.messages.create(
            model=self.model,
            max_tokens=self.config.max_tokens,
            thinking={"type": "adaptive"},
            output_config={
                "effort": self.effort.value,
                "format": {"type": "json_schema", "schema": transform_schema(answer)},
            },
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            cache_control={"type": "ephemeral"},
            messages=messages,
            **extra,
        )
        return response

    def _answer[T: pydantic.BaseModel](
        self,
        response: BetaMessage,
        answer: type[T],
        turns: list[Turn],
        fail: Callable[[Failure, str], Outcome[T]],
    ) -> Outcome[T]:
        text = "".join(b.text for b in response.content if b.type == "text")
        if not text.strip():
            return fail(Failure.INVALID_OUTPUT, "the answer has no text")
        try:
            value = answer.model_validate_json(text)
        except pydantic.ValidationError as e:
            return fail(Failure.INVALID_OUTPUT, _clip(str(e)))
        outcome = Outcome(value, None, "", tuple(turns), self._alert(turns))
        log.info("llm.answered", **_summary(outcome))
        return outcome

    def _turn(self, response: BetaMessage, seconds: float) -> Turn:
        u = response.usage
        details = u.output_tokens_details
        iterations = u.iterations or []
        if iterations:
            # Each hop priced at its own model: a declined hop is billed too.
            cost = sum(
                self._cost(
                    it.model or self.model,
                    it.input_tokens,
                    it.cache_read_input_tokens,
                    it.cache_creation_input_tokens,
                    it.output_tokens,
                )
                for it in iterations
                if it.type in ("message", "fallback_message")
            )
        else:
            cost = self._cost(
                response.model,
                u.input_tokens,
                u.cache_read_input_tokens or 0,
                u.cache_creation_input_tokens or 0,
                u.output_tokens,
            )
        return Turn(
            model=response.model,
            stop_reason=response.stop_reason,
            seconds=round(seconds, 1),
            input_tokens=u.input_tokens,
            cache_read_tokens=u.cache_read_input_tokens or 0,
            cache_write_tokens=u.cache_creation_input_tokens or 0,
            output_tokens=u.output_tokens,
            thinking_tokens=details.thinking_tokens if details else None,
            tools=tuple(b.name for b in response.content if b.type == "tool_use"),
            cost_usd=cost,
            fallback=any(it.type == "fallback_message" for it in iterations),
            request_id=response._request_id,
        )

    def _cost(self, model: str, uncached: int, read: int, written: int, output: int) -> float:
        p = self._prices(model)
        dollars = (
            uncached * p.input + read * p.cache_read + written * p.cache_write + output * p.output
        )
        return dollars / 1_000_000

    def _prices(self, model: str) -> TokenPrices:
        prices = self.config.prices
        if model in prices:
            return prices[model]
        return max(prices.values(), key=lambda p: (p.output, p.input))

    def _alert(self, turns: Sequence[Turn]) -> bool:
        return sum(t.cost_usd for t in turns) > self.config.cost_alert_usd


def _echoed(content: Sequence[BetaContentBlock]) -> list[BetaContentBlock]:
    """The assistant turn as it goes back. Before a mid-output fallback's boundary only the
    text survives: the declined model's thinking and tool calls are not echoed (and its tool
    calls are not run)."""
    boundary = max((i for i, b in enumerate(content) if b.type == "fallback"), default=-1)
    return [b for i, b in enumerate(content) if i > boundary or (i < boundary and b.type == "text")]


def _run_tools(
    content: Sequence[BetaContentBlock], tools: dict[str, Tool[Any]]
) -> list[BetaContentBlockParam]:
    """One result per tool call, all in one user message."""
    results: list[BetaContentBlockParam] = []
    for block in content:
        if block.type != "tool_use":
            continue
        tool = tools.get(block.name)
        output, error = tool.call(block.input) if tool else (f"unknown tool {block.name}", True)
        result: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id, "content": output}
        if error:
            result["is_error"] = True
        results.append(cast(BetaContentBlockParam, result))
    return results


def _clip(text: str) -> str:
    return text if len(text) <= DETAIL_CHARS else text[: DETAIL_CHARS - 1] + "…"


def _summary(outcome: Outcome[Any]) -> dict[str, Any]:
    return {
        "model": outcome.model,
        "fallback": outcome.fallback,
        "turns": len(outcome.turns),
        "seconds": round(sum(t.seconds for t in outcome.turns), 1),
        "cost_usd": round(outcome.cost_usd, 4),
        "cost_alert": outcome.cost_alert,
        "request_ids": [t.request_id for t in outcome.turns],
    }

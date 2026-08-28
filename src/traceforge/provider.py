from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from openai import APIConnectionError, APIError, APITimeoutError, AsyncOpenAI, RateLimitError

from traceforge.config import Settings
from traceforge.model_reasoning import resolve_reasoning_capability
from traceforge.models import ReasoningEffort, ToolCall


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        category: str = "provider",
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.category = category


@dataclass(slots=True)
class ModelResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    reasoning_content: str | None = None
    preserve_empty_content: bool = False

    def as_assistant_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": self.content if self.preserve_empty_content else self.content or None,
        }
        if self.reasoning_content is not None:
            message["reasoning_content"] = self.reasoning_content
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in self.tool_calls
            ]
        return message


class ModelProvider(Protocol):
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        reasoning_effort: ReasoningEffort = ReasoningEffort.AUTO,
    ) -> ModelResponse: ...


class OpenAICompatibleProvider:
    def __init__(self, settings: Settings) -> None:
        self.model = settings.model
        self._reasoning = resolve_reasoning_capability(
            settings.model, base_url=settings.base_url
        )
        # TraceForge owns retry policy so one visible attempt is exactly one HTTP request.
        # The SDK defaults to two hidden retries and a 600-second read timeout, which can
        # otherwise leave a local run apparently idle for far too long.
        self._client = AsyncOpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            max_retries=0,
            timeout=settings.model_request_timeout,
        )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        reasoning_effort: ReasoningEffort = ReasoningEffort.AUTO,
    ) -> ModelResponse:
        self._reasoning.validate(reasoning_effort)
        try:
            deepseek = self._reasoning.transport == "deepseek_chat"
            kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": _messages_for_transport(messages, deepseek=deepseek),
            }
            if tools:
                kwargs["tools"] = tools
                if not deepseek:
                    kwargs["tool_choice"] = "auto"
            if reasoning_effort is not ReasoningEffort.AUTO:
                if deepseek:
                    enabled = reasoning_effort is not ReasoningEffort.NONE
                    kwargs["extra_body"] = {
                        "thinking": {"type": "enabled" if enabled else "disabled"}
                    }
                    if enabled:
                        kwargs["reasoning_effort"] = reasoning_effort.value
                else:
                    kwargs["reasoning_effort"] = reasoning_effort.value
            response = await self._client.chat.completions.create(**kwargs)
            choice = response.choices[0]
            calls: list[ToolCall] = []
            for call in choice.message.tool_calls or []:
                try:
                    arguments = json.loads(call.function.arguments)
                except json.JSONDecodeError as exc:
                    raise ProviderError(
                        f"Model returned invalid JSON for tool {call.function.name}: {exc}",
                        category="protocol",
                    ) from exc
                if not isinstance(arguments, dict):
                    raise ProviderError(
                        f"Tool arguments for {call.function.name} must be a JSON object",
                        category="protocol",
                    )
                calls.append(ToolCall(id=call.id, name=call.function.name, arguments=arguments))
            return ModelResponse(
                content=choice.message.content or "",
                tool_calls=calls,
                finish_reason=choice.finish_reason,
                reasoning_content=(
                    _reasoning_content(choice.message, has_tool_calls=bool(calls))
                    if deepseek
                    else None
                ),
                preserve_empty_content=deepseek,
            )
        except RateLimitError as exc:
            raise ProviderError(
                "Model rate limit was reached",
                retryable=True,
                category="rate_limit",
            ) from exc
        except APITimeoutError as exc:
            raise ProviderError(
                "Model request timed out",
                retryable=True,
                category="timeout",
            ) from exc
        except APIConnectionError as exc:
            raise ProviderError(
                "Model connection failed",
                retryable=True,
                category="connection",
            ) from exc
        except APIError as exc:
            status_code = getattr(exc, "status_code", None)
            retryable = isinstance(status_code, int) and status_code >= 500
            raise ProviderError(
                (
                    "Model service returned a temporary server error"
                    if retryable
                    else "Model request was rejected"
                ),
                retryable=retryable,
                category="server" if retryable else "request",
            ) from exc
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Model provider returned an unreadable response ({type(exc).__name__})",
                category="provider_contract",
            ) from exc


class ScriptedProvider:
    """Deterministic provider used by integration tests and local product demos."""

    def __init__(
        self,
        responses: list[ModelResponse],
        *,
        delay_seconds: float = 0,
        repeat: bool = False,
    ) -> None:
        self._script = responses.copy()
        self._responses = self._script.copy()
        self._delay_seconds = delay_seconds
        self._repeat = repeat
        self.requests: list[tuple[list[dict[str, Any]], list[dict[str, Any]] | None]] = []
        self.reasoning_efforts: list[ReasoningEffort] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        reasoning_effort: ReasoningEffort = ReasoningEffort.AUTO,
    ) -> ModelResponse:
        self.requests.append((messages, tools))
        self.reasoning_efforts.append(reasoning_effort)
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        if not self._responses and self._repeat:
            self._responses = self._script.copy()
        if not self._responses:
            raise ProviderError("Scripted provider has no remaining responses")
        return self._responses.pop(0)


def _messages_for_transport(
    messages: list[dict[str, Any]], *, deepseek: bool
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for original in messages:
        message = original.copy()
        if deepseek:
            if message.get("role") == "assistant" and message.get("tool_calls"):
                if message.get("content") is None:
                    message["content"] = ""
                message.setdefault("reasoning_content", "")
        else:
            # Raw provider reasoning is replay-only state, never a portable message field.
            message.pop("reasoning_content", None)
        prepared.append(message)
    return prepared


def _reasoning_content(message: Any, *, has_tool_calls: bool) -> str | None:
    value = getattr(message, "reasoning_content", None)
    if isinstance(value, str):
        return value
    return "" if has_tool_calls else None

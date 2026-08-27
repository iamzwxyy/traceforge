from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from openai import APIConnectionError, APIError, APITimeoutError, AsyncOpenAI, RateLimitError

from traceforge.config import Settings
from traceforge.models import ToolCall


class ProviderError(RuntimeError):
    pass


@dataclass(slots=True)
class ModelResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None

    def as_assistant_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content or None}
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
    ) -> ModelResponse: ...


class OpenAICompatibleProvider:
    def __init__(self, settings: Settings) -> None:
        self.model = settings.model
        self._client = AsyncOpenAI(api_key=settings.api_key, base_url=settings.base_url)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        delay = 1.0
        for attempt in range(3):
            try:
                kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
                if tools:
                    kwargs.update({"tools": tools, "tool_choice": "auto"})
                response = await self._client.chat.completions.create(**kwargs)
                choice = response.choices[0]
                calls: list[ToolCall] = []
                for call in choice.message.tool_calls or []:
                    try:
                        arguments = json.loads(call.function.arguments)
                    except json.JSONDecodeError as exc:
                        raise ProviderError(
                            f"Model returned invalid JSON for tool {call.function.name}: {exc}"
                        ) from exc
                    if not isinstance(arguments, dict):
                        raise ProviderError(
                            f"Tool arguments for {call.function.name} must be a JSON object"
                        )
                    calls.append(
                        ToolCall(id=call.id, name=call.function.name, arguments=arguments)
                    )
                return ModelResponse(
                    content=choice.message.content or "",
                    tool_calls=calls,
                    finish_reason=choice.finish_reason,
                )
            except (APIConnectionError, APITimeoutError, RateLimitError) as exc:
                if attempt == 2:
                    raise ProviderError(
                        f"Model request failed after three attempts: {exc}"
                    ) from exc
                await asyncio.sleep(delay)
                delay *= 2
            except APIError as exc:
                raise ProviderError(f"Model request was rejected: {exc}") from exc
        raise AssertionError("Retry loop ended unexpectedly")


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

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        self.requests.append((messages, tools))
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        if not self._responses and self._repeat:
            self._responses = self._script.copy()
        if not self._responses:
            raise ProviderError("Scripted provider has no remaining responses")
        return self._responses.pop(0)

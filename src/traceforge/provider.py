from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from openai import APIConnectionError, APIError, APITimeoutError, AsyncOpenAI, RateLimitError

from traceforge.config import Settings
from traceforge.models import ToolCall


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
    ) -> ModelResponse:
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
            )
        except RateLimitError as exc:
            raise ProviderError(
                f"Model rate limit was reached: {exc}",
                retryable=True,
                category="rate_limit",
            ) from exc
        except APITimeoutError as exc:
            raise ProviderError(
                f"Model request timed out: {exc}",
                retryable=True,
                category="timeout",
            ) from exc
        except APIConnectionError as exc:
            raise ProviderError(
                f"Model connection failed: {exc}",
                retryable=True,
                category="connection",
            ) from exc
        except APIError as exc:
            status_code = getattr(exc, "status_code", None)
            retryable = isinstance(status_code, int) and status_code >= 500
            raise ProviderError(
                (
                    f"Model service returned a temporary server error: {exc}"
                    if retryable
                    else f"Model request was rejected: {exc}"
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

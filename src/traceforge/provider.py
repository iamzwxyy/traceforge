from __future__ import annotations

import asyncio
import email.utils
import inspect
import json
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)
from openai.types.chat import ChatCompletionChunk

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
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.category = category
        self.retry_after_seconds = retry_after_seconds


_MAX_HTTP_BODY_BYTES = 32 * 1024 * 1024
_MAX_HTTP_CHUNKS = 10_000
_MAX_SSE_EVENT_BYTES = 16 * 1024 * 1024
_MAX_SSE_EVENTS = 10_000
_MAX_SSE_LINES = 50_000
_MAX_RETRY_AFTER_SECONDS = 120.0


class _ResponseGuardError(RuntimeError):
    pass


class _BoundedResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, limit: int) -> None:
        self._stream = stream
        self._limit = limit
        self._received = 0
        self._chunks = 0
        self._closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        primary_error: BaseException | None = None
        try:
            async for chunk in self._stream:
                self._chunks += 1
                if self._chunks > _MAX_HTTP_CHUNKS:
                    raise _ResponseGuardError(
                        "Model response used too many transport chunks"
                    )
                self._received += len(chunk)
                if self._received > self._limit:
                    raise _ResponseGuardError("Model response exceeded the HTTP body limit")
                yield chunk
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                await self.aclose()
            except Exception:
                # SDK error handling can consume a 4xx/5xx body before returning a response
                # object to our context manager. Retry once here so a transient close failure
                # cannot orphan that otherwise unreachable transport, while the primary
                # cancellation or boundary error remains authoritative.
                try:
                    await self.aclose()
                except Exception:
                    if primary_error is None:
                        raise

    async def aclose(self) -> None:
        if self._closed:
            return
        await self._stream.aclose()
        self._closed = True


async def _guard_http_response(response: httpx.Response) -> None:
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"", "identity"}:
        raise _ResponseGuardError("Compressed model responses are not accepted")
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise _ResponseGuardError("Model response Content-Length is invalid") from exc
        if declared_length < 0 or declared_length > _MAX_HTTP_BODY_BYTES:
            raise _ResponseGuardError("Model response exceeded the HTTP body limit")
    if not isinstance(response.stream, httpx.AsyncByteStream):
        raise _ResponseGuardError("Model response stream is not asynchronous")
    response.stream = _BoundedResponseStream(response.stream, _MAX_HTTP_BODY_BYTES)


def _build_http_client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        headers={"Accept-Encoding": "identity"},
        event_hooks={"response": [_guard_http_response]},
    )


@dataclass(slots=True)
class ModelResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    reasoning_content: str | None = None
    preserve_empty_content: bool = False
    output_stream_id: str | None = None

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


@dataclass(slots=True)
class ModelToolCallDelta:
    index: int
    id: str = ""
    name: str = ""
    arguments: str = ""
    type: str | None = None


@dataclass(slots=True)
class ModelStreamDelta:
    content: str = ""
    tool_calls: list[ModelToolCallDelta] = field(default_factory=list)
    finish_reason: str | None = None


ModelDeltaSink = Callable[[ModelStreamDelta], Awaitable[None]]


class StreamingModelProvider(Protocol):
    supports_streaming: bool

    async def stream_complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        reasoning_effort: ReasoningEffort = ReasoningEffort.AUTO,
        on_delta: ModelDeltaSink,
    ) -> ModelResponse: ...


async def close_model_provider(provider: ModelProvider) -> None:
    close = getattr(provider, "close", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


class OpenAICompatibleProvider:
    supports_streaming = True

    def __init__(self, settings: Settings) -> None:
        self.model = settings.model
        self._request_timeout = settings.model_request_timeout
        self._reasoning = resolve_reasoning_capability(settings.model, base_url=settings.base_url)
        # TraceForge owns retry policy so one visible attempt is exactly one HTTP request.
        # The SDK defaults to two hidden retries and a 600-second read timeout, which can
        # otherwise leave a local run apparently idle for far too long.
        self._http_client = _build_http_client(settings.model_request_timeout)
        self._client = AsyncOpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            max_retries=0,
            timeout=settings.model_request_timeout,
            http_client=self._http_client,
        )
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._client, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result
        if not self._http_client.is_closed:
            await self._http_client.aclose()

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        reasoning_effort: ReasoningEffort = ReasoningEffort.AUTO,
    ) -> ModelResponse:
        self._reasoning.validate(reasoning_effort)
        if self._supports_bounded_raw_stream():
            return await self.stream_complete(
                messages,
                tools,
                reasoning_effort=reasoning_effort,
                on_delta=_discard_delta,
            )
        try:
            deepseek = self._reasoning.transport == "deepseek_chat"
            kwargs = self._completion_kwargs(
                messages,
                tools,
                reasoning_effort=reasoning_effort,
                deepseek=deepseek,
            )
            async with asyncio.timeout(self._request_timeout):
                response = await self._client.chat.completions.create(**kwargs)
            choice = response.choices[0]
            if deepseek and choice.finish_reason == "insufficient_system_resource":
                raise ProviderError(
                    "DeepSeek reported insufficient system resources",
                    retryable=True,
                    category="server",
                )
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
            retryable = (
                _api_error_is_retryable(exc)
                if getattr(exc, "response", None) is not None
                else True
            )
            raise ProviderError(
                "Model rate limit was reached",
                retryable=retryable,
                category="rate_limit" if retryable else "request",
                retry_after_seconds=(
                    _api_error_retry_after_seconds(exc) if retryable else None
                ),
            ) from exc
        except (APITimeoutError, TimeoutError) as exc:
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
            retryable = _api_error_is_retryable(exc)
            raise ProviderError(
                (
                    "Model service returned a temporary server error"
                    if retryable
                    else "Model request was rejected"
                ),
                retryable=retryable,
                category="server" if retryable else "request",
                retry_after_seconds=(
                    _api_error_retry_after_seconds(exc) if retryable else None
                ),
            ) from exc
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Model provider returned an unreadable response ({type(exc).__name__})",
                category="provider_contract",
            ) from exc

    async def stream_complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        reasoning_effort: ReasoningEffort = ReasoningEffort.AUTO,
        on_delta: ModelDeltaSink,
    ) -> ModelResponse:
        self._reasoning.validate(reasoning_effort)
        deepseek = self._reasoning.transport == "deepseek_chat"
        accumulator = _ChatCompletionAccumulator(deepseek=deepseek)
        try:
            kwargs = self._completion_kwargs(
                messages,
                tools,
                reasoning_effort=reasoning_effort,
                deepseek=deepseek,
            )
            kwargs["stream"] = True
            async with asyncio.timeout(self._request_timeout):
                if self._supports_bounded_raw_stream():
                    await self._consume_bounded_stream(
                        kwargs,
                        accumulator=accumulator,
                        on_delta=on_delta,
                    )
                else:
                    stream = await self._client.chat.completions.create(**kwargs)
                    async with stream:
                        async for chunk in stream:
                            delta = accumulator.add(chunk)
                            if delta.content or delta.tool_calls or delta.finish_reason:
                                await on_delta(delta)
            return accumulator.finish()
        except _ResponseGuardError as exc:
            raise ProviderError(
                "Model response exceeded a safe transport boundary",
                category="protocol",
            ) from exc
        except RateLimitError as exc:
            retryable = (
                _api_error_is_retryable(exc)
                if getattr(exc, "response", None) is not None
                else True
            )
            raise ProviderError(
                "Model rate limit was reached",
                retryable=retryable,
                category="rate_limit" if retryable else "request",
                retry_after_seconds=(
                    _api_error_retry_after_seconds(exc) if retryable else None
                ),
            ) from exc
        except (APITimeoutError, httpx.TimeoutException, TimeoutError) as exc:
            raise ProviderError(
                "Model request timed out",
                retryable=True,
                category="timeout",
            ) from exc
        except (APIConnectionError, httpx.TransportError) as exc:
            if _find_response_guard_error(exc) is not None:
                raise ProviderError(
                    "Model response exceeded a safe transport boundary",
                    category="protocol",
                ) from exc
            raise ProviderError(
                "Model connection failed",
                retryable=True,
                category="connection",
            ) from exc
        except APIError as exc:
            retryable = _api_error_is_retryable(exc)
            raise ProviderError(
                (
                    "Model service returned a temporary server error"
                    if retryable
                    else "Model request was rejected"
                ),
                retryable=retryable,
                category="server" if retryable else "request",
                retry_after_seconds=(
                    _api_error_retry_after_seconds(exc) if retryable else None
                ),
            ) from exc
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Model provider returned an unreadable stream ({type(exc).__name__})",
                category="provider_contract",
            ) from exc

    def _completion_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        reasoning_effort: ReasoningEffort,
        deepseek: bool,
    ) -> dict[str, Any]:
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
                kwargs["extra_body"] = {"thinking": {"type": "enabled" if enabled else "disabled"}}
                if enabled:
                    kwargs["reasoning_effort"] = reasoning_effort.value
            else:
                kwargs["reasoning_effort"] = reasoning_effort.value
        return kwargs

    def _supports_bounded_raw_stream(self) -> bool:
        return hasattr(self._client.chat.completions, "with_streaming_response")

    async def _consume_bounded_stream(
        self,
        kwargs: dict[str, Any],
        *,
        accumulator: _ChatCompletionAccumulator,
        on_delta: ModelDeltaSink,
    ) -> None:
        framer = _BoundedSSEFramer()
        done = False
        completions = self._client.chat.completions.with_streaming_response
        response_context = completions.create(**kwargs)
        response = await response_context.__aenter__()
        primary_error: BaseException | None = None
        try:
            async for chunk in response.iter_bytes(chunk_size=64 * 1024):
                for payload in framer.feed(chunk):
                    if payload.strip() == b"[DONE]":
                        done = True
                        break
                    parsed = _parse_sse_payload(payload)
                    model_chunk = _validated_stream_chunk(
                        parsed,
                        deepseek=self._reasoning.transport == "deepseek_chat",
                    )
                    delta = accumulator.add(model_chunk, raw_size=len(payload))
                    if delta.content or delta.tool_calls or delta.finish_reason:
                        await on_delta(delta)
                if done:
                    break
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                await response.close()
            except Exception:
                if primary_error is None:
                    raise
        if not done:
            framer.finish()


class _BoundedSSEFramer:
    def __init__(self) -> None:
        self._buffer = bytearray()
        self._data_lines: list[bytes] = []
        self._event_bytes = 0
        self._events = 0
        self._lines = 0
        self._total_bytes = 0

    def feed(self, chunk: bytes) -> list[bytes]:
        if not isinstance(chunk, bytes):
            raise ProviderError("Model stream yielded non-byte data", category="protocol")
        self._total_bytes += len(chunk)
        if self._total_bytes > _MAX_HTTP_BODY_BYTES:
            raise _ResponseGuardError("Model stream exceeded the HTTP body limit")
        self._buffer.extend(chunk)
        emitted: list[bytes] = []
        consumed = 0
        while consumed < len(self._buffer):
            line_feed = self._buffer.find(b"\n", consumed)
            carriage_return = self._buffer.find(b"\r", consumed)
            endings = [index for index in (line_feed, carriage_return) if index >= 0]
            if not endings:
                break
            ending = min(endings)
            if self._buffer[ending] == 13 and ending + 1 == len(self._buffer):
                break
            delimiter_length = (
                2
                if self._buffer[ending] == 13
                and ending + 1 < len(self._buffer)
                and self._buffer[ending + 1] == 10
                else 1
            )
            line = bytes(self._buffer[consumed:ending])
            consumed = ending + delimiter_length
            emitted.extend(self._accept_line(line))
        if consumed:
            del self._buffer[:consumed]
        if self._event_bytes + len(self._buffer) > _MAX_SSE_EVENT_BYTES:
            raise _ResponseGuardError("Model stream event exceeded the size limit")
        return emitted

    def finish(self) -> None:
        if self._buffer or self._data_lines or self._event_bytes:
            raise ProviderError(
                "Model stream ended in the middle of an event",
                retryable=True,
                category="connection",
            )

    def _accept_line(self, line: bytes) -> list[bytes]:
        self._lines += 1
        if self._lines > _MAX_SSE_LINES:
            raise _ResponseGuardError("Model stream used too many SSE lines")
        self._event_bytes += len(line) + 1
        if self._event_bytes > _MAX_SSE_EVENT_BYTES:
            raise _ResponseGuardError("Model stream event exceeded the size limit")
        if line:
            if line.startswith(b":"):
                return []
            field, separator, value = line.partition(b":")
            if separator and value.startswith(b" "):
                value = value[1:]
            if field == b"data":
                self._data_lines.append(value)
            return []

        self._events += 1
        if self._events > _MAX_SSE_EVENTS:
            raise _ResponseGuardError("Model stream used too many SSE events")
        payload = b"\n".join(self._data_lines) if self._data_lines else None
        self._data_lines = []
        self._event_bytes = 0
        return [payload] if payload is not None else []


def _parse_sse_payload(payload: bytes) -> Mapping[str, Any]:
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError("Model stream returned invalid JSON", category="protocol") from exc
    if not isinstance(parsed, Mapping):
        raise ProviderError("Model stream event must be a JSON object", category="protocol")
    error = parsed.get("error")
    if error is not None:
        error_type = str(error.get("type", "")) if isinstance(error, Mapping) else ""
        error_code = str(error.get("code", "")) if isinstance(error, Mapping) else ""
        markers = {error_type.strip().lower(), error_code.strip().lower()}
        transient = bool(
            markers
            & {
                "server_error",
                "rate_limit",
                "rate_limit_error",
                "overloaded_error",
                "insufficient_system_resource",
            }
        )
        rate_limited = bool(markers & {"rate_limit", "rate_limit_error"})
        raise ProviderError(
            (
                "Model stream reported a temporary server error"
                if transient
                else "Model stream returned an error response"
            ),
            retryable=transient,
            category=("rate_limit" if rate_limited else "server" if transient else "request"),
        )
    return parsed


def _is_deepseek_resource_event(payload: Mapping[str, Any]) -> bool:
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        return False
    choice = choices[0]
    return (
        isinstance(choice, Mapping)
        and choice.get("finish_reason") == "insufficient_system_resource"
    )


def _validated_stream_chunk(
    payload: Mapping[str, Any],
    *,
    deepseek: bool,
) -> ChatCompletionChunk:
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            if "index" in choice and type(choice["index"]) is not int:
                raise ProviderError(
                    "Streaming choice index must be an integer",
                    category="protocol",
                )
            delta = choice.get("delta")
            tool_calls = delta.get("tool_calls") if isinstance(delta, Mapping) else None
            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    if (
                        isinstance(tool_call, Mapping)
                        and "index" in tool_call
                        and type(tool_call["index"]) is not int
                    ):
                        raise ProviderError(
                            "Streaming tool call index must be an integer",
                            category="protocol",
                        )

    resource_event = deepseek and _is_deepseek_resource_event(payload)
    candidate: Mapping[str, Any] = payload
    if resource_event:
        assert isinstance(choices, list)
        normalized_choice = dict(choices[0])
        normalized_choice["finish_reason"] = "stop"
        normalized = dict(payload)
        normalized["choices"] = [normalized_choice]
        candidate = normalized
    try:
        model_chunk = ChatCompletionChunk.model_validate(candidate)
    except Exception as exc:
        raise ProviderError(
            "Model stream returned an invalid event shape",
            category="protocol",
        ) from exc
    if resource_event:
        raise ProviderError(
            "DeepSeek reported insufficient system resources",
            retryable=True,
            category="server",
        )
    return model_chunk


def _find_response_guard_error(exc: BaseException) -> _ResponseGuardError | None:
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, _ResponseGuardError):
            return current
        current = current.__cause__ or current.__context__
    return None


async def _discard_delta(_delta: ModelStreamDelta) -> None:
    return None


class ScriptedProvider:
    """Deterministic provider used by integration tests and local product demos."""

    def __init__(
        self,
        responses: list[ModelResponse],
        *,
        delay_seconds: float = 0,
        repeat: bool = False,
        streaming: bool = False,
    ) -> None:
        self._script = responses.copy()
        self._responses = self._script.copy()
        self._delay_seconds = delay_seconds
        self._repeat = repeat
        self.supports_streaming = streaming
        self.requests: list[tuple[list[dict[str, Any]], list[dict[str, Any]] | None]] = []
        self.reasoning_efforts: list[ReasoningEffort] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        reasoning_effort: ReasoningEffort = ReasoningEffort.AUTO,
    ) -> ModelResponse:
        response = await self._next_response(messages, tools, reasoning_effort)
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        return response

    async def stream_complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        reasoning_effort: ReasoningEffort = ReasoningEffort.AUTO,
        on_delta: ModelDeltaSink,
    ) -> ModelResponse:
        if not self.supports_streaming:
            raise ProviderError("Scripted streaming was not enabled", category="provider_contract")
        response = await self._next_response(messages, tools, reasoning_effort)
        deltas = _scripted_deltas(response)
        delay = self._delay_seconds / max(1, len(deltas))
        for delta in deltas:
            if delay:
                await asyncio.sleep(delay)
            await on_delta(delta)
        return response

    async def _next_response(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        reasoning_effort: ReasoningEffort,
    ) -> ModelResponse:
        self.requests.append((messages, tools))
        self.reasoning_efforts.append(reasoning_effort)
        if not self._responses and self._repeat:
            self._responses = self._script.copy()
        if not self._responses:
            raise ProviderError("Scripted provider has no remaining responses")
        return self._responses.pop(0)


@dataclass(slots=True)
class _ToolParts:
    id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list)
    arguments_length: int = 0
    type: str | None = None


class _ChatCompletionAccumulator:
    _ALLOWED_FINISH_REASONS = frozenset({"stop", "tool_calls", "length", "content_filter"})
    _DEEPSEEK_RESOURCE_FINISH_REASON = "insufficient_system_resource"
    _MAX_CONTENT = 200_000
    _MAX_REASONING = 400_000
    _MAX_ARGUMENTS = 1_000_000
    _MAX_METADATA = 2_000
    _MAX_TOOL_CALLS = 64
    _MAX_TOTAL_CHARACTERS = 2_000_000
    _MAX_CHUNKS = 10_000
    _MAX_TOOL_FRAGMENTS = 10_000

    def __init__(self, *, deepseek: bool) -> None:
        self._deepseek = deepseek
        self._content: list[str] = []
        self._content_length = 0
        self._reasoning: list[str] = []
        self._reasoning_length = 0
        self._reasoning_seen = False
        self._tools: dict[int, _ToolParts] = {}
        self._finish_reason: str | None = None
        self._total_characters = 0
        self._chunk_count = 0
        self._tool_fragment_count = 0

    def add(self, chunk: Any, *, raw_size: int | None = None) -> ModelStreamDelta:
        self._chunk_count += 1
        if self._chunk_count > self._MAX_CHUNKS:
            raise ProviderError("Streaming response used too many chunks", category="protocol")
        count_known_fields = raw_size is None
        if raw_size is not None:
            self._consume_units(raw_size)
        choices = getattr(chunk, "choices", None)
        if choices == []:
            return ModelStreamDelta()
        if not isinstance(choices, list):
            raise ProviderError("Streaming choices must be a list", category="protocol")
        choice_index = getattr(choices[0], "index", None) if len(choices) == 1 else None
        if type(choice_index) is not int or choice_index != 0:
            raise ProviderError(
                "Streaming responses must contain exactly choice index 0",
                category="protocol",
            )
        choice = choices[0]
        if self._finish_reason is not None:
            raise ProviderError(
                "Streaming response continued after its terminal chunk",
                category="protocol",
            )
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason is not None:
            if not isinstance(finish_reason, str):
                raise ProviderError("Streaming finish_reason must be a string", category="protocol")
            allowed = finish_reason in self._ALLOWED_FINISH_REASONS or (
                self._deepseek and finish_reason == self._DEEPSEEK_RESOURCE_FINISH_REASON
            )
            if not allowed:
                raise ProviderError(
                    "Streaming response used an unsupported finish reason",
                    category="protocol",
                )
            if self._finish_reason and self._finish_reason != finish_reason:
                raise ProviderError("Streaming response changed finish_reason", category="protocol")
            self._finish_reason = finish_reason
        delta = choice.delta
        if getattr(delta, "function_call", None) is not None:
            raise ProviderError(
                "Deprecated streaming function_call output is not supported",
                category="protocol",
            )
        content = getattr(delta, "content", None)
        if content is not None and not isinstance(content, str):
            raise ProviderError("Streaming content must be a string", category="protocol")
        refusal = getattr(delta, "refusal", None)
        if refusal is not None and not isinstance(refusal, str):
            raise ProviderError("Streaming refusal must be a string", category="protocol")
        public_content = (content or "") + (refusal or "")
        if public_content:
            if count_known_fields:
                self._consume_characters(public_content)
            self._content.append(public_content)
            self._content_length += len(public_content)
            if self._content_length > self._MAX_CONTENT:
                raise ProviderError(
                    "Streaming response content exceeded the size limit",
                    category="protocol",
                )
        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning is not None and not isinstance(reasoning, str):
            raise ProviderError("Streaming private reasoning must be a string", category="protocol")
        if count_known_fields and isinstance(reasoning, str):
            self._consume_characters(reasoning)
        if self._deepseek and isinstance(reasoning, str):
            self._reasoning_seen = True
            self._reasoning.append(reasoning)
            self._reasoning_length += len(reasoning)
            if self._reasoning_length > self._MAX_REASONING:
                raise ProviderError(
                    "Streaming private reasoning exceeded the size limit",
                    category="protocol",
                )

        public_tools: list[ModelToolCallDelta] = []
        raw_calls = getattr(delta, "tool_calls", None)
        if raw_calls is not None and not isinstance(raw_calls, list):
            raise ProviderError("Streaming tool_calls must be a list", category="protocol")
        for raw_call in raw_calls or []:
            self._tool_fragment_count += 1
            if self._tool_fragment_count > self._MAX_TOOL_FRAGMENTS:
                raise ProviderError(
                    "Streaming response used too many tool fragments",
                    category="protocol",
                )
            index = getattr(raw_call, "index", None)
            if type(index) is not int or index < 0 or index >= self._MAX_TOOL_CALLS:
                raise ProviderError("Streaming tool call index is invalid", category="protocol")
            parts = self._tools.setdefault(index, _ToolParts())
            raw_type = getattr(raw_call, "type", None)
            if raw_type is not None:
                if not isinstance(raw_type, str):
                    raise ProviderError(
                        "Streaming tool call type must be a string", category="protocol"
                    )
                if raw_type != "function" or (parts.type and parts.type != raw_type):
                    raise ProviderError("Streaming tool call type is invalid", category="protocol")
                parts.type = raw_type
            raw_call_id = getattr(raw_call, "id", None)
            if raw_call_id is not None and not isinstance(raw_call_id, str):
                raise ProviderError("Streaming tool id must be a string", category="protocol")
            call_id = raw_call_id or ""
            function = getattr(raw_call, "function", None)
            raw_name = getattr(function, "name", None)
            raw_arguments = getattr(function, "arguments", None)
            if raw_name is not None and not isinstance(raw_name, str):
                raise ProviderError("Streaming tool name must be a string", category="protocol")
            if raw_arguments is not None and not isinstance(raw_arguments, str):
                raise ProviderError(
                    "Streaming tool arguments must be a string", category="protocol"
                )
            name = raw_name or ""
            arguments = raw_arguments or ""
            if count_known_fields:
                self._consume_characters(call_id, name, arguments)
            parts.id = _append_metadata(parts.id, call_id)
            parts.name = _append_metadata(parts.name, name)
            if len(parts.id) > self._MAX_METADATA or len(parts.name) > self._MAX_METADATA:
                raise ProviderError(
                    "Streaming tool metadata exceeded the size limit",
                    category="protocol",
                )
            if arguments:
                parts.arguments.append(arguments)
                parts.arguments_length += len(arguments)
            if parts.arguments_length > self._MAX_ARGUMENTS:
                raise ProviderError(
                    "Streaming tool arguments exceeded the size limit",
                    category="protocol",
                )
            public_tools.append(
                ModelToolCallDelta(
                    index=index,
                    id=call_id,
                    name=name,
                    arguments=arguments,
                    type=raw_type,
                )
            )
        return ModelStreamDelta(
            content=public_content,
            tool_calls=public_tools,
            finish_reason=str(finish_reason) if finish_reason else None,
        )

    def finish(self) -> ModelResponse:
        if self._finish_reason is None:
            raise ProviderError(
                "Model stream ended before a terminal finish reason",
                retryable=True,
                category="connection",
            )
        if self._finish_reason == self._DEEPSEEK_RESOURCE_FINISH_REASON:
            raise ProviderError(
                "DeepSeek reported insufficient system resources",
                retryable=True,
                category="server",
            )
        if self._finish_reason in {"length", "content_filter"}:
            raise ProviderError(
                f"Model stream ended with incomplete output ({self._finish_reason})",
                category="protocol",
            )
        indexes = sorted(self._tools)
        if indexes != list(range(len(indexes))):
            raise ProviderError(
                "Streaming tool call indexes are not contiguous", category="protocol"
            )
        calls: list[ToolCall] = []
        for index in indexes:
            parts = self._tools[index]
            if not parts.id or not parts.name or parts.type not in {None, "function"}:
                raise ProviderError(
                    "Streaming tool call metadata is incomplete", category="protocol"
                )
            try:
                arguments = json.loads("".join(parts.arguments))
            except json.JSONDecodeError as exc:
                raise ProviderError(
                    f"Model returned invalid JSON for tool {parts.name}: {exc}",
                    category="protocol",
                ) from exc
            if not isinstance(arguments, dict):
                raise ProviderError(
                    f"Tool arguments for {parts.name} must be a JSON object",
                    category="protocol",
                )
            calls.append(ToolCall(id=parts.id, name=parts.name, arguments=arguments))
        if len({call.id for call in calls}) != len(calls):
            raise ProviderError("Streaming tool call ids must be unique", category="protocol")
        if calls and self._finish_reason != "tool_calls":
            raise ProviderError(
                "Streaming tool calls ended without finish_reason=tool_calls",
                category="protocol",
            )
        if not calls and self._finish_reason == "tool_calls":
            raise ProviderError(
                "Streaming response declared missing tool calls", category="protocol"
            )
        return ModelResponse(
            content="".join(self._content),
            tool_calls=calls,
            finish_reason=self._finish_reason,
            reasoning_content=(
                "".join(self._reasoning)
                if self._deepseek and self._reasoning_seen
                else ("" if self._deepseek and calls else None)
            ),
            preserve_empty_content=self._deepseek,
        )

    def _consume_characters(self, *fragments: str) -> None:
        self._consume_units(sum(len(fragment) for fragment in fragments))

    def _consume_units(self, amount: int) -> None:
        self._total_characters += amount
        if self._total_characters > self._MAX_TOTAL_CHARACTERS:
            raise ProviderError(
                "Streaming response exceeded the total size limit", category="protocol"
            )


def _append_metadata(current: str, fragment: str) -> str:
    if not fragment or fragment == current:
        return current
    if fragment.startswith(current):
        return fragment
    return current + fragment


def _api_error_is_retryable(exc: APIError) -> bool:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    retry_after = _api_error_retry_after_seconds(exc)
    if retry_after is not None and retry_after > _MAX_RETRY_AFTER_SECONDS:
        return False
    should_retry = headers.get("x-should-retry") if headers is not None else None
    if isinstance(should_retry, str):
        normalized = should_retry.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and (status in {408, 409, 429} or status >= 500)


def _api_error_retry_after_seconds(exc: APIError) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    retry_after_ms = headers.get("retry-after-ms")
    try:
        milliseconds = float(retry_after_ms)
    except (TypeError, ValueError):
        pass
    else:
        seconds = milliseconds / 1_000
        return seconds if math.isfinite(seconds) and seconds > 0 else None

    raw = headers.get("retry-after")
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        try:
            parsed = email.utils.parsedate_tz(raw)
            if parsed is None:
                return None
            seconds = float(email.utils.mktime_tz(parsed) - time.time())
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def _scripted_deltas(response: ModelResponse) -> list[ModelStreamDelta]:
    deltas: list[ModelStreamDelta] = []
    for start in range(0, len(response.content), 32):
        deltas.append(ModelStreamDelta(content=response.content[start : start + 32]))
    for index, call in enumerate(response.tool_calls):
        arguments = json.dumps(call.arguments, ensure_ascii=False)
        pieces = [arguments[start : start + 32] for start in range(0, len(arguments), 32)] or [""]
        for piece_index, piece in enumerate(pieces):
            deltas.append(
                ModelStreamDelta(
                    tool_calls=[
                        ModelToolCallDelta(
                            index=index,
                            id=call.id if piece_index == 0 else "",
                            name=call.name if piece_index == 0 else "",
                            arguments=piece,
                            type="function" if piece_index == 0 else None,
                        )
                    ]
                )
            )
    deltas.append(
        ModelStreamDelta(
            finish_reason=response.finish_reason
            or ("tool_calls" if response.tool_calls else "stop")
        )
    )
    return deltas


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

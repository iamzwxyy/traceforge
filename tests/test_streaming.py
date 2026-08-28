# ruff: noqa: RUF001  # Chinese streaming fixtures intentionally use Chinese punctuation.

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import httpx
import pytest

import traceforge.agent as agent_module
import traceforge.provider as provider_module
from traceforge.agent import AgentManager
from traceforge.config import Settings
from traceforge.models import ConversationTurn, EventType, RunRecord, RunState, ToolCall
from traceforge.provider import (
    ModelResponse,
    ModelStreamDelta,
    ModelToolCallDelta,
    OpenAICompatibleProvider,
    ProviderError,
    ScriptedProvider,
)
from traceforge.storage import Storage
from traceforge.streaming import (
    StableStreamingRedactor,
    boundary_safe_json_dumps,
    contains_redactable_json_secret,
    contains_redactable_serialized_json_secret,
    contains_secret_representation,
    json_string_field_prefix,
    redact_text,
)


def _choice(*, delta: object, finish_reason: str | None = None) -> object:
    return SimpleNamespace(index=0, delta=delta, finish_reason=finish_reason)


def _chunk(*, delta: object | None = None, finish_reason: str | None = None) -> object:
    return SimpleNamespace(
        choices=[_choice(delta=delta or SimpleNamespace(), finish_reason=finish_reason)]
    )


def _tool_delta(
    index: int,
    *,
    call_id: str = "",
    name: str = "",
    arguments: str = "",
    call_type: str | None = None,
) -> object:
    return SimpleNamespace(
        index=index,
        id=call_id or None,
        type=call_type,
        function=SimpleNamespace(name=name or None, arguments=arguments or None),
    )


async def _capture_delta(
    target: list[ModelStreamDelta],
    delta: ModelStreamDelta,
) -> None:
    target.append(delta)


class _FakeAsyncStream:
    def __init__(self, items: list[object]) -> None:
        self._items = iter(items)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            item = next(self._items)
        except StopIteration:
            raise StopAsyncIteration from None
        if isinstance(item, BaseException):
            raise item
        return item

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.closed = True


class _HeartbeatAsyncStream:
    def __init__(self) -> None:
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0.005)
        return SimpleNamespace(choices=[])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.closed = True


class _BlockingAsyncStream:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled provider stream resumed")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.closed = True


class _FakeCompletions:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes.copy()
        self.kwargs: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        self.kwargs.append(kwargs)
        return self.outcomes.pop(0)


class _HTTPBodyStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _BlockingHTTPBodyStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        self.entered.set()
        await asyncio.Event().wait()
        yield b"unreachable"

    async def aclose(self) -> None:
        self.closed = True


class _RetryableCloseHTTPBodyStream(_BlockingHTTPBodyStream):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            raise httpx.CloseError("injected first close failure")
        self.closed = True


class _RetryableCloseHTTPBytesStream(_HTTPBodyStream):
    def __init__(self, chunks: list[bytes]) -> None:
        super().__init__(chunks)
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            raise httpx.CloseError("injected first close failure")
        self.closed = True


class _CancelDuringCloseHTTPBytesStream(_HTTPBodyStream):
    def __init__(self, chunks: list[bytes]) -> None:
        super().__init__(chunks)
        self.close_entered = asyncio.Event()
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            self.close_entered.set()
            await asyncio.Event().wait()
        self.closed = True


def _sse(payload: object) -> bytes:
    if payload == "[DONE]":
        return b"data: [DONE]\n\n"
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


def _raw_provider(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    handler,
) -> OpenAICompatibleProvider:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(
        transport=transport,
        timeout=settings.model_request_timeout,
        headers={"Accept-Encoding": "identity"},
        event_hooks={"response": [provider_module._guard_http_response]},
    )
    monkeypatch.setattr(
        provider_module,
        "_build_http_client",
        lambda _timeout: http_client,
    )
    return OpenAICompatibleProvider(settings)


def _raw_chunk(
    *,
    delta: dict[str, object],
    finish_reason: str | None = None,
) -> dict[str, object]:
    return {
        "id": "chunk-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }


def test_json_string_prefix_decodes_incomplete_escapes_and_surrogates() -> None:
    assert json_string_field_prefix('{"content":"hello\\n', "content").value == "hello\n"
    held = json_string_field_prefix('{"content":"face \\ud83d', "content")
    assert held.value == "face "
    assert held.complete is False
    completed = json_string_field_prefix(
        '{"content":"face \\ud83d\\ude00"}', "content"
    )
    assert completed.value == "face 😀"
    assert completed.complete is True
    assert json_string_field_prefix('{"content":"bad\\q', "content").valid is False
    assert json_string_field_prefix('{"other":"hidden"}', "content").value is None


@pytest.mark.asyncio
async def test_raw_sdk_stream_surfaces_refusal_and_closes_transport(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _HTTPBodyStream(
        [
            _sse(_raw_chunk(delta={"role": "assistant", "refusal": "Request refused."})),
            _sse(_raw_chunk(delta={}, finish_reason="stop")),
            _sse("[DONE]"),
        ]
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=body,
        )

    provider = _raw_provider(settings, monkeypatch, handler)
    try:
        response = await provider.complete([{"role": "user", "content": "unsafe"}])
    finally:
        await provider.close()

    assert response.content == "Request refused."
    assert json.loads(requests[0].content)["stream"] is True
    assert requests[0].headers["accept-encoding"] == "identity"
    assert body.closed is True


@pytest.mark.asyncio
async def test_provider_rejects_a_credential_synthesized_by_compact_json_boundaries(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = '--- END WORKSPACE GUIDANCE ---"},{"role":"user"'
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("Credential-bearing JSON must be rejected before HTTP transmission")

    provider = _raw_provider(replace(settings, api_key=credential), monkeypatch, handler)
    try:
        with pytest.raises(ProviderError) as raised:
            await provider.complete(
                [
                    {
                        "role": "user",
                        "content": "--- END WORKSPACE GUIDANCE ---",
                    },
                    {"role": "user", "content": "next request"},
                ]
            )
    finally:
        await provider.close()

    assert raised.value.category == "credential_boundary"
    assert credential not in str(raised.value)
    assert requests == []


@pytest.mark.asyncio
async def test_provider_guards_the_sdk_reordered_wire_body_before_transport(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = 'tail-fragment"}],"model":"custom"'
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("The SDK-transformed credential must not reach transport")

    provider = _raw_provider(
        replace(settings, api_key=credential, model="custom"),
        monkeypatch,
        handler,
    )
    try:
        with pytest.raises(ProviderError) as raised:
            await provider.complete([{"role": "user", "content": "tail-fragment"}])
    finally:
        await provider.close()

    assert raised.value.category == "credential_boundary"
    assert credential not in str(raised.value)
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "retryable", "category"),
    [
        ("server_error", True, "server"),
        ("invalid_request_error", False, "request"),
    ],
)
async def test_raw_sdk_stream_classifies_in_band_error_envelopes(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    error_type: str,
    retryable: bool,
    category: str,
) -> None:
    body = _HTTPBodyStream(
        [
            _sse(
                {
                    "error": {
                        "message": "provider detail must stay private",
                        "type": error_type,
                        "code": error_type,
                    }
                }
            )
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=body,
        )

    provider = _raw_provider(settings, monkeypatch, handler)
    try:
        with pytest.raises(ProviderError) as raised:
            await provider.stream_complete([], on_delta=lambda _delta: asyncio.sleep(0))
    finally:
        await provider.close()

    assert raised.value.retryable is retryable
    assert raised.value.category == category
    assert "provider detail" not in str(raised.value)
    assert body.closed is True


@pytest.mark.asyncio
async def test_raw_sdk_stream_honors_rate_limit_retry_override(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"x-should-retry": "false"},
            json={"error": {"message": "slow down", "type": "rate_limit_error"}},
        )

    provider = _raw_provider(settings, monkeypatch, handler)
    try:
        with pytest.raises(ProviderError) as raised:
            await provider.stream_complete([], on_delta=lambda _delta: asyncio.sleep(0))
    finally:
        await provider.close()

    assert raised.value.retryable is False
    assert raised.value.category == "request"


@pytest.mark.asyncio
async def test_rate_limit_policy_survives_a_retryable_pre_context_close_failure(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _RetryableCloseHTTPBytesStream(
        [b'{"error":{"message":"slow down","type":"rate_limit_error"}}']
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={
                "content-type": "application/json",
                "x-should-retry": "false",
            },
            stream=body,
        )

    provider = _raw_provider(settings, monkeypatch, handler)
    try:
        with pytest.raises(ProviderError) as raised:
            await provider.stream_complete(
                [], on_delta=lambda _delta: asyncio.sleep(0)
            )
    finally:
        await provider.close()

    assert raised.value.retryable is False
    assert raised.value.category == "request"
    assert body.close_calls >= 2
    assert body.closed is True


@pytest.mark.asyncio
async def test_raw_transport_rejects_oversized_and_compressed_bodies_before_json(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_module, "_MAX_HTTP_BODY_BYTES", 64)
    oversized = _HTTPBodyStream([b"data: " + b"x" * 128])

    def oversized_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=oversized,
        )

    oversized_provider = _raw_provider(settings, monkeypatch, oversized_handler)
    try:
        with pytest.raises(ProviderError) as oversized_error:
            await oversized_provider.stream_complete(
                [], on_delta=lambda _delta: asyncio.sleep(0)
            )
    finally:
        await oversized_provider.close()

    assert oversized_error.value.category == "protocol"
    assert oversized.closed is True

    oversized_error_body = _HTTPBodyStream(
        [
            b'{"error":{"message":"'
            + b"x" * 128
            + b'","type":"rate_limit_error"}}'
        ]
    )

    def oversized_error_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"content-type": "application/json"},
            stream=oversized_error_body,
        )

    oversized_error_provider = _raw_provider(
        settings,
        monkeypatch,
        oversized_error_handler,
    )
    try:
        with pytest.raises(ProviderError) as guarded_error:
            await oversized_error_provider.stream_complete(
                [], on_delta=lambda _delta: asyncio.sleep(0)
            )
    finally:
        await oversized_error_provider.close()

    assert guarded_error.value.category == "protocol"
    assert guarded_error.value.retryable is False
    assert oversized_error_body.closed is True

    compressed = _HTTPBodyStream([b"not actually compressed"])

    def compressed_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "content-encoding": "gzip",
            },
            stream=compressed,
        )

    compressed_provider = _raw_provider(settings, monkeypatch, compressed_handler)
    try:
        with pytest.raises(ProviderError) as compressed_error:
            await compressed_provider.stream_complete(
                [], on_delta=lambda _delta: asyncio.sleep(0)
            )
    finally:
        await compressed_provider.close()

    assert compressed_error.value.category == "protocol"
    assert compressed.closed is True


@pytest.mark.asyncio
async def test_oversized_error_body_retries_close_before_sdk_loses_the_response(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_module, "_MAX_HTTP_BODY_BYTES", 64)
    body = _RetryableCloseHTTPBytesStream(
        [
            b'{"error":{"message":"'
            + b"x" * 128
            + b'","type":"rate_limit_error"}}'
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"content-type": "application/json"},
            stream=body,
        )

    provider = _raw_provider(settings, monkeypatch, handler)
    try:
        with pytest.raises(ProviderError) as raised:
            await provider.stream_complete(
                [], on_delta=lambda _delta: asyncio.sleep(0)
            )
    finally:
        await provider.close()

    assert raised.value.category == "protocol"
    assert raised.value.retryable is False
    assert body.close_calls >= 2
    assert body.closed is True


@pytest.mark.asyncio
async def test_user_cancellation_wins_if_it_arrives_during_boundary_cleanup(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_module, "_MAX_HTTP_BODY_BYTES", 64)
    body = _CancelDuringCloseHTTPBytesStream([b"data: " + b"x" * 128])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=body,
        )

    provider = _raw_provider(settings, monkeypatch, handler)
    task = asyncio.create_task(
        provider.stream_complete([], on_delta=lambda _delta: asyncio.sleep(0))
    )
    await asyncio.wait_for(body.close_entered.wait(), timeout=1)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await provider.close()

    assert body.close_calls >= 2
    assert body.closed is True


@pytest.mark.asyncio
async def test_raw_transport_cancellation_closes_the_active_response(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _BlockingHTTPBodyStream()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=body,
        )

    provider = _raw_provider(settings, monkeypatch, handler)
    task = asyncio.create_task(
        provider.stream_complete([], on_delta=lambda _delta: asyncio.sleep(0))
    )
    await asyncio.wait_for(body.entered.wait(), timeout=1)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await provider.close()

    assert body.closed is True


@pytest.mark.asyncio
async def test_raw_cancellation_survives_a_retryable_transport_close_failure(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _RetryableCloseHTTPBodyStream()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=body,
        )

    provider = _raw_provider(settings, monkeypatch, handler)
    task = asyncio.create_task(
        provider.stream_complete([], on_delta=lambda _delta: asyncio.sleep(0))
    )
    await asyncio.wait_for(body.entered.wait(), timeout=1)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await provider.close()

    assert body.close_calls >= 2
    assert body.closed is True


@pytest.mark.asyncio
async def test_raw_deepseek_resource_finish_reason_is_retryable_before_sdk_validation(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _HTTPBodyStream(
        [
            _sse(
                _raw_chunk(
                    delta={},
                    finish_reason="insufficient_system_resource",
                )
            )
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=body,
        )

    provider = _raw_provider(
        replace(
            settings,
            model="deepseek-v4-pro",
            base_url="https://api.deepseek.com/v1",
        ),
        monkeypatch,
        handler,
    )
    try:
        with pytest.raises(ProviderError) as raised:
            await provider.stream_complete(
                [], on_delta=lambda _delta: asyncio.sleep(0)
            )
    finally:
        await provider.close()

    assert raised.value.category == "server"
    assert raised.value.retryable is True
    assert body.closed is True


@pytest.mark.asyncio
async def test_raw_deepseek_resource_event_still_validates_the_remaining_schema(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed = _raw_chunk(
        delta={},
        finish_reason="insufficient_system_resource",
    )
    malformed["object"] = "wrong.object"
    body = _HTTPBodyStream([_sse(malformed)])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=body,
        )

    provider = _raw_provider(
        replace(
            settings,
            model="deepseek-v4-pro",
            base_url="https://api.deepseek.com/v1",
        ),
        monkeypatch,
        handler,
    )
    try:
        with pytest.raises(ProviderError) as raised:
            await provider.stream_complete(
                [], on_delta=lambda _delta: asyncio.sleep(0)
            )
    finally:
        await provider.close()

    assert raised.value.category == "protocol"
    assert raised.value.retryable is False
    assert body.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("choice_index", "tool_index"),
    [
        (False, 0),
        ("0", 0),
        (0.0, 0),
        (0, False),
        (0, "0"),
        (0, 0.0),
    ],
)
async def test_raw_stream_rejects_indexes_the_sdk_would_coerce(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    choice_index: object,
    tool_index: object,
) -> None:
    payload = _raw_chunk(
        delta={
            "tool_calls": [
                {
                    "index": tool_index,
                    "id": "strict-index",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ]
        },
    )
    choices = payload["choices"]
    assert isinstance(choices, list)
    choice = choices[0]
    assert isinstance(choice, dict)
    choice["index"] = choice_index
    body = _HTTPBodyStream([_sse(payload)])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=body,
        )

    provider = _raw_provider(settings, monkeypatch, handler)
    try:
        with pytest.raises(ProviderError) as raised:
            await provider.stream_complete(
                [], on_delta=lambda _delta: asyncio.sleep(0)
            )
    finally:
        await provider.close()

    assert raised.value.category == "protocol"
    assert raised.value.retryable is False
    assert body.closed is True


@pytest.mark.asyncio
async def test_real_raw_deepseek_stream_handles_interleaved_tools_reasoning_and_usage_tail(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage_tail = {
        "id": "chunk-usage",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "deepseek-v4-pro",
        "choices": [],
        "usage": {
            "prompt_tokens": 4,
            "completion_tokens": 3,
            "total_tokens": 7,
        },
    }
    body = _HTTPBodyStream(
        [
            _sse(
                _raw_chunk(
                    delta={
                        "reasoning_content": "private-a",
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "call-b",
                                "type": "function",
                                "function": {
                                    "name": "read_",
                                    "arguments": '{"path":"b',
                                },
                            }
                        ],
                    }
                )
            ),
            _sse(
                _raw_chunk(
                    delta={
                        "reasoning_content": "private-b",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-a",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path":"a.txt"}',
                                },
                            },
                            {
                                "index": 1,
                                "function": {
                                    "name": "file",
                                    "arguments": '.txt"}',
                                },
                            },
                        ],
                    }
                )
            ),
            _sse(_raw_chunk(delta={}, finish_reason="tool_calls")),
            _sse(usage_tail),
            _sse("[DONE]"),
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=body,
        )

    provider = _raw_provider(
        replace(
            settings,
            model="deepseek-v4-pro",
            base_url="https://api.deepseek.com/v1",
        ),
        monkeypatch,
        handler,
    )
    deltas: list[ModelStreamDelta] = []
    try:
        response = await provider.stream_complete(
            [], on_delta=lambda delta: _capture_delta(deltas, delta)
        )
    finally:
        await provider.close()

    assert response.reasoning_content == "private-aprivate-b"
    assert response.content == ""
    assert [(call.id, call.name, call.arguments) for call in response.tool_calls] == [
        ("call-a", "read_file", {"path": "a.txt"}),
        ("call-b", "read_file", {"path": "b.txt"}),
    ]
    assert all(delta.content == "" for delta in deltas)
    assert body.closed is True


@pytest.mark.parametrize(
    "secret",
    ["owner-only-key-123456", "sk-abcdefghijklmnop"],
)
def test_streaming_redactor_never_releases_a_secret_at_any_chunk_boundary(
    secret: str,
) -> None:
    configured = secret if not secret.startswith("sk-") else "different-owner-key"
    content = f"safe prefix {secret} safe suffix"
    expected = redact_text(content, api_key=configured)

    for split in range(1, len(content)):
        redactor = StableStreamingRedactor(api_key=configured)
        first = redactor.update(content[:split])
        second = redactor.update(content)
        final = redactor.finish(content)
        assert secret not in first
        assert secret not in second
        assert first == expected[: len(first)]
        assert second == expected[: len(second)]
        assert final == expected


@pytest.mark.parametrize(
    "content",
    [
        "safe owner-only-key-123456sk-abcdefghijkl suffix",
        "safe xsk-abcdefghijkl- suffix",
        "sk-abcdefghijkl.more owner-only-key-123456",
    ],
)
def test_streaming_redactor_is_stable_for_adjacent_secrets_and_every_prefix(
    content: str,
) -> None:
    configured = "owner-only-key-123456"
    expected = redact_text(content, api_key=configured)
    redactor = StableStreamingRedactor(api_key=configured)

    for end in range(1, len(content) + 1):
        stable = redactor.update(content[:end])
        assert stable == expected[: len(stable)]
        assert "owner-only" not in stable
        assert "sk-" not in stable

    assert redactor.finish(content) == expected


def test_redaction_merges_an_exact_key_with_its_token_shaped_subrange() -> None:
    configured = "owner-fragment-sk-abcdefghijkl"
    content = f"safe {configured} suffix"
    expected = redact_text(content, api_key=configured)

    assert redact_text(content, api_key=configured) == expected
    redactor = StableStreamingRedactor(api_key=configured)
    for end in range(1, len(content) + 1):
        stable = redactor.update(content[:end])
        assert stable == expected[: len(stable)]
        assert "owner-fragment" not in stable
    assert redactor.finish(content) == expected


def test_streaming_redaction_uses_merged_overlapping_key_occurrences() -> None:
    configured = "a123456789012345678901234567890a"
    content = configured + configured[1:]
    expected = redact_text(content, api_key=configured)
    redactor = StableStreamingRedactor(api_key=configured)

    for end in range(1, len(content) + 1):
        stable = redactor.update(content[:end])
        assert stable == expected[: len(stable)]
        assert configured[1:-1] not in stable
    assert redactor.finish(content) == expected


def test_redaction_marker_collision_never_releases_configured_key() -> None:
    configured = "[REDACTED]"
    content = f"safe {configured} suffix"
    expected = redact_text(content, api_key=configured)
    redactor = StableStreamingRedactor(api_key=configured)

    assert configured not in expected
    for end in range(1, len(content) + 1):
        stable = redactor.update(content[:end])
        assert stable == expected[: len(stable)]
        assert configured not in stable
    assert redactor.finish(content) == expected


def test_nested_json_secret_detection_includes_object_keys_and_token_shapes() -> None:
    configured = 'owner"secret\\tail\tsegment'

    assert contains_redactable_json_secret(
        {"safe": [{configured: "value"}]}, api_key=configured
    )
    assert contains_redactable_json_secret(
        {"safe": ("prefix sk-abcdefghijklmnop suffix",)}, api_key=""
    )
    assert not contains_redactable_json_secret(
        {"safe": [False, 0, None, "ordinary text"]}, api_key=configured
    )


def test_boundary_safe_json_cannot_synthesize_a_single_line_key_from_structure() -> None:
    configured = 'foo", "start_line": 1'
    value = {"path": "foo", "start_line": 1}

    assert not contains_redactable_json_secret(value, api_key=configured)
    assert configured in json.dumps(value, ensure_ascii=False)
    assert configured not in boundary_safe_json_dumps(value)
    assert not contains_redactable_serialized_json_secret(
        value, api_key=configured
    )


def test_nested_json_escape_levels_are_detected_without_a_fixed_depth() -> None:
    configured = 'owner"secret\\tail\tsegment'
    candidate = configured
    for _ in range(12):
        candidate = json.dumps(candidate, ensure_ascii=False)[1:-1]

    assert contains_secret_representation(candidate, api_key=configured)
    assert contains_redactable_serialized_json_secret(
        {"nested_json": candidate}, api_key=configured
    )


def test_assistant_storage_redacts_escaped_credentials_before_json_serialization(
    settings: Settings,
    storage: Storage,
) -> None:
    configured = 'owner"secret\\tail\tsegment'
    protected_settings = replace(settings, api_key=configured)
    manager = AgentManager(protected_settings, storage, ScriptedProvider([]))
    response = ModelResponse(
        content=f"public {configured}",
        tool_calls=[
            ToolCall(
                id="escaped-secret",
                name="custom_tool",
                arguments={configured: {"value": configured}},
            )
        ],
    )

    stored_message = manager._assistant_message_for_storage(response)
    run = RunRecord(
        id="escaped-credential-storage",
        task="store a sanitized provider response",
        workspace=str(settings.workspace),
        messages=[stored_message],
    )
    storage.create_run(run)
    persisted_message = storage.get_run(run.id).messages[0]
    wire_calls = persisted_message["tool_calls"]
    assert isinstance(wire_calls, list)
    arguments = json.loads(wire_calls[0]["function"]["arguments"])
    marker = redact_text(configured, api_key=configured)

    assert configured not in persisted_message["content"]
    assert arguments == {marker: {"value": marker}}
    escaped = json.dumps(configured, ensure_ascii=False)[1:-1].encode()
    for database_file in settings.data_dir.glob("test.db*"):
        assert escaped not in database_file.read_bytes()


def test_private_reasoning_rejects_a_key_equal_to_the_legacy_marker(
    settings: Settings,
    storage: Storage,
) -> None:
    configured = "[REDACTED]-owner-key-123456"
    manager = AgentManager(
        replace(settings, api_key=configured),
        storage,
        ScriptedProvider([]),
    )

    with pytest.raises(
        ProviderError,
        match="Provider-private replay state could not be stored safely",
    ):
        manager._assistant_message_for_storage(
            ModelResponse(content="safe", reasoning_content=configured)
        )


def test_redacted_tool_argument_keys_fail_closed_instead_of_colliding(
    settings: Settings,
    storage: Storage,
) -> None:
    configured = 'owner"secret\\tail\tsegment'
    marker = redact_text(configured, api_key=configured)
    manager = AgentManager(
        replace(settings, api_key=configured),
        storage,
        ScriptedProvider([]),
    )

    for arguments in (
        {configured: "secret-key", marker: "existing-marker"},
        {marker: "existing-marker", configured: "secret-key"},
    ):
        with pytest.raises(
            ProviderError,
            match="Provider tool calls could not be stored safely",
        ):
            manager._assistant_message_for_storage(
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="colliding-key",
                            name="custom_tool",
                            arguments=arguments,
                        )
                    ]
                )
            )


@pytest.mark.asyncio
async def test_openai_stream_assembles_interleaved_tools_and_private_reasoning(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _FakeAsyncStream(
        [
            _chunk(delta=SimpleNamespace(content=None, reasoning_content="private-a")),
            _chunk(
                delta=SimpleNamespace(
                    content=None,
                    reasoning_content="private-b",
                    tool_calls=[
                        _tool_delta(
                            1,
                            call_id="call-b",
                            name="read_",
                            arguments='{"path":',
                            call_type="function",
                        ),
                        _tool_delta(
                            0,
                            call_id="call-a",
                            name="respond_",
                            arguments='{"content":"hel',
                            call_type="function",
                        ),
                    ],
                )
            ),
            _chunk(
                delta=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        _tool_delta(0, name="to_user", arguments='lo"}'),
                        _tool_delta(1, name="file", arguments='"a.py"}'),
                    ],
                )
            ),
            _chunk(delta=SimpleNamespace(), finish_reason="tool_calls"),
            SimpleNamespace(choices=[]),
        ]
    )
    completions = _FakeCompletions([stream])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(provider_module, "AsyncOpenAI", lambda **_kwargs: client)
    provider = OpenAICompatibleProvider(
        replace(
            settings,
            model="deepseek-v4-pro",
            base_url="https://api.deepseek.com/v1",
        )
    )
    seen: list[ModelStreamDelta] = []

    response = await provider.stream_complete(
        [{"role": "user", "content": "hello"}],
        [{"type": "function"}],
        on_delta=lambda delta: _record_delta(seen, delta),
    )

    assert [call.name for call in response.tool_calls] == ["respond_to_user", "read_file"]
    assert response.tool_calls[0].arguments == {"content": "hello"}
    assert response.tool_calls[1].arguments == {"path": "a.py"}
    assert response.reasoning_content == "private-aprivate-b"
    assert "private" not in json.dumps([asdict(delta) for delta in seen], default=str)
    assert completions.kwargs[0]["stream"] is True
    assert stream.closed is True


async def _record_delta(seen: list[ModelStreamDelta], delta: ModelStreamDelta) -> None:
    seen.append(delta)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "category"),
    [
        (None, "connection"),
        (httpx.ReadTimeout("slow body"), "timeout"),
        (httpx.ReadError("broken body"), "connection"),
    ],
)
async def test_openai_stream_fails_closed_on_truncation_and_transport_errors(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException | None,
    category: str,
) -> None:
    items: list[object] = [_chunk(delta=SimpleNamespace(content="partial"))]
    if failure is not None:
        items.append(failure)
    stream = _FakeAsyncStream(items)
    completions = _FakeCompletions([stream])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(provider_module, "AsyncOpenAI", lambda **_kwargs: client)
    provider = OpenAICompatibleProvider(settings)

    with pytest.raises(ProviderError) as raised:
        await provider.stream_complete([], on_delta=lambda _delta: asyncio.sleep(0))

    assert raised.value.retryable is True
    assert raised.value.category == category
    assert stream.closed is True


@pytest.mark.asyncio
async def test_openai_stream_timeout_is_a_wall_clock_deadline_across_heartbeats(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _HeartbeatAsyncStream()
    completions = _FakeCompletions([stream])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(provider_module, "AsyncOpenAI", lambda **_kwargs: client)
    provider = OpenAICompatibleProvider(replace(settings, model_request_timeout=0.03))  # type: ignore[arg-type]

    with pytest.raises(ProviderError) as raised:
        await provider.stream_complete([], on_delta=lambda _delta: asyncio.sleep(0))

    assert raised.value.retryable is True
    assert raised.value.category == "timeout"
    assert stream.closed is True


@pytest.mark.asyncio
async def test_openai_stream_cancellation_closes_transport_and_propagates(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _BlockingAsyncStream()
    completions = _FakeCompletions([stream])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(provider_module, "AsyncOpenAI", lambda **_kwargs: client)
    provider = OpenAICompatibleProvider(settings)

    task = asyncio.create_task(
        provider.stream_complete([], on_delta=lambda _delta: asyncio.sleep(0))
    )
    await asyncio.wait_for(stream.entered.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed is True


@pytest.mark.asyncio
async def test_deepseek_resource_finish_reason_is_retryable_and_provider_specific(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deepseek_stream = _FakeAsyncStream(
        [_chunk(delta=SimpleNamespace(), finish_reason="insufficient_system_resource")]
    )
    other_stream = _FakeAsyncStream(
        [_chunk(delta=SimpleNamespace(), finish_reason="insufficient_system_resource")]
    )
    completions = _FakeCompletions([deepseek_stream, other_stream])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(provider_module, "AsyncOpenAI", lambda **_kwargs: client)

    deepseek = OpenAICompatibleProvider(
        replace(settings, model="deepseek-v4-pro", base_url="https://api.deepseek.com/v1")
    )
    with pytest.raises(ProviderError) as resource_error:
        await deepseek.stream_complete([], on_delta=lambda _delta: asyncio.sleep(0))
    assert resource_error.value.retryable is True
    assert resource_error.value.category == "server"

    other = OpenAICompatibleProvider(settings)
    with pytest.raises(ProviderError) as protocol_error:
        await other.stream_complete([], on_delta=lambda _delta: asyncio.sleep(0))
    assert protocol_error.value.retryable is False
    assert protocol_error.value.category == "protocol"


@pytest.mark.asyncio
async def test_openai_stream_enforces_one_global_character_budget(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        provider_module._ChatCompletionAccumulator,
        "_MAX_TOTAL_CHARACTERS",
        10,
    )
    stream = _FakeAsyncStream(
        [
            _chunk(delta=SimpleNamespace(content="123456", reasoning_content=None)),
            _chunk(delta=SimpleNamespace(content=None, reasoning_content="abcdef")),
        ]
    )
    completions = _FakeCompletions([stream])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(provider_module, "AsyncOpenAI", lambda **_kwargs: client)
    provider = OpenAICompatibleProvider(
        replace(settings, model="deepseek-v4-pro", base_url="https://api.deepseek.com/v1")
    )

    with pytest.raises(ProviderError) as raised:
        await provider.stream_complete([], on_delta=lambda _delta: asyncio.sleep(0))

    assert raised.value.category == "protocol"
    assert stream.closed is True


@pytest.mark.parametrize(
    ("status", "header", "expected"),
    [
        (408, None, True),
        (409, None, True),
        (429, "false", False),
        (400, "true", True),
        (503, "false", False),
        (400, None, False),
    ],
)
def test_api_error_retry_policy_honors_status_and_explicit_header(
    status: int,
    header: str | None,
    expected: bool,
) -> None:
    headers = {} if header is None else {"x-should-retry": header}
    error = SimpleNamespace(
        status_code=status,
        response=SimpleNamespace(headers=headers),
    )
    assert provider_module._api_error_is_retryable(error) is expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("headers", "retryable", "delay"),
    [
        ({"retry-after": "120"}, True, 120.0),
        ({"retry-after": "121"}, False, 121.0),
        ({"retry-after-ms": "7500"}, True, 7.5),
    ],
)
def test_api_error_retry_policy_bounds_server_directed_delay(
    headers: dict[str, str],
    retryable: bool,
    delay: float,
) -> None:
    error = SimpleNamespace(
        status_code=429,
        response=SimpleNamespace(headers=headers),
    )

    assert provider_module._api_error_is_retryable(error) is retryable  # type: ignore[arg-type]
    assert provider_module._api_error_retry_after_seconds(error) == delay  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "items",
    [
        pytest.param(
            [
                _chunk(
                    delta=SimpleNamespace(
                        function_call=SimpleNamespace(name="legacy", arguments="{}")
                    )
                )
            ],
            id="deprecated-function-call",
        ),
        pytest.param(
            [
                _chunk(delta=SimpleNamespace(), finish_reason="stop"),
                _chunk(delta=SimpleNamespace(content="late output")),
            ],
            id="output-after-terminal",
        ),
        pytest.param(
            [_chunk(delta=SimpleNamespace(), finish_reason="future_unknown_reason")],
            id="unknown-finish-reason",
        ),
        pytest.param(
            [_chunk(delta=SimpleNamespace(content="x" * 200_001))],
            id="oversized-content",
        ),
        pytest.param(
            [_chunk(delta=SimpleNamespace(content=["not", "text"]))],
            id="non-string-content",
        ),
        pytest.param(
            [_chunk(delta=SimpleNamespace(reasoning_content=["not", "text"]))],
            id="non-string-private-reasoning",
        ),
        pytest.param(
            [
                _chunk(
                    delta=SimpleNamespace(
                        tool_calls=[
                            SimpleNamespace(
                                index=True,
                                id="call",
                                type="function",
                                function=SimpleNamespace(name="tool", arguments="{}"),
                            )
                        ]
                    )
                )
            ],
            id="boolean-tool-index",
        ),
    ],
)
async def test_openai_stream_rejects_unsafe_protocol_shapes(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    items: list[object],
) -> None:
    stream = _FakeAsyncStream(items)
    completions = _FakeCompletions([stream])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(provider_module, "AsyncOpenAI", lambda **_kwargs: client)
    provider = OpenAICompatibleProvider(settings)

    with pytest.raises(ProviderError) as raised:
        await provider.stream_complete([], on_delta=lambda _delta: asyncio.sleep(0))

    assert raised.value.retryable is False
    assert raised.value.category == "protocol"
    assert stream.closed is True


def test_stream_accumulator_rejects_zero_length_tool_fragment_floods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        provider_module._ChatCompletionAccumulator,
        "_MAX_TOOL_FRAGMENTS",
        2,
    )
    accumulator = provider_module._ChatCompletionAccumulator(deepseek=False)
    chunk = _chunk(
        delta=SimpleNamespace(
            tool_calls=[
                _tool_delta(0, call_id="call", name="tool", call_type="function"),
                _tool_delta(0),
                _tool_delta(0),
            ]
        )
    )

    with pytest.raises(ProviderError, match="too many tool fragments"):
        accumulator.add(chunk)


@pytest.mark.asyncio
async def test_agent_streams_one_canonical_direct_answer_without_secret_persistence(
    settings: Settings,
    storage: Storage,
) -> None:
    secret = settings.api_key
    content = (
        "这是一个足够长的流式回答，用来证明模型结束之前就会发布安全的增量内容。"
        f"敏感值 {secret} 不得进入事件数据库。"
        "后续文本继续增长，让至少两个批次都能被观察到并最终合并成一个正式气泡。"
    )
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="answer",
                        name="respond_to_user",
                        arguments={"content": content},
                    )
                ]
            )
        ],
        delay_seconds=0.3,
        streaming=True,
    )
    manager = AgentManager(settings, storage, provider)

    run = await manager.start_run("请流式回答")
    async with asyncio.timeout(2):
        while not any(  # noqa: ASYNC110
            event.type is EventType.ASSISTANT_OUTPUT_DELTA
            for event in storage.get_events(run.id)
        ):
            await asyncio.sleep(0.01)
    assert storage.get_run(run.id).state is RunState.PLANNING

    completed = await manager.wait(run.id)
    events = storage.get_events(run.id)
    started = [event for event in events if event.type is EventType.ASSISTANT_OUTPUT_STARTED]
    deltas = [event for event in events if event.type is EventType.ASSISTANT_OUTPUT_DELTA]
    streamed = "".join(str(event.payload["delta"]) for event in deltas)
    output_completed = next(
        event for event in events if event.type is EventType.ASSISTANT_OUTPUT_COMPLETED
    )
    turn_completed = next(event for event in events if event.type is EventType.TURN_COMPLETED)

    assert completed.state is RunState.ANSWERED
    assert len(started) == 1
    assert len(deltas) >= 2
    assert streamed == redact_text(content, api_key=secret)
    assert output_completed.payload["content"] == streamed
    assert turn_completed.payload["summary"] == streamed
    assert turn_completed.payload["final_stream_id"] == started[0].payload["stream_id"]
    persisted = json.dumps(
        [event.model_dump(mode="json") for event in events], ensure_ascii=False
    )
    assert secret not in persisted
    for database_file in settings.data_dir.glob("test.db*"):
        assert secret.encode() not in database_file.read_bytes()


@pytest.mark.asyncio
async def test_overlapping_configured_key_occurrences_never_reach_durable_deltas(
    settings: Settings,
    storage: Storage,
) -> None:
    configured = "a123456789012345678901234567890a"
    leak_fragment = configured[1:-1]
    content = "Z" * 30 + configured + configured[1:]

    class OverlappingSecretProvider:
        supports_streaming = True

        async def complete(self, *_args: object, **_kwargs: object) -> ModelResponse:
            raise AssertionError("stream_complete should be used")

        async def stream_complete(self, *_args: object, on_delta, **_kwargs: object):
            arguments = json.dumps({"content": content})
            await on_delta(
                ModelStreamDelta(
                    tool_calls=[
                        ModelToolCallDelta(
                            index=0,
                            id="overlap-answer",
                            name="respond_to_user",
                            arguments=arguments,
                            type="function",
                        )
                    ]
                )
            )
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="overlap-answer",
                        name="respond_to_user",
                        arguments={"content": content},
                    )
                ]
            )

    protected_settings = replace(settings, api_key=configured)
    manager = AgentManager(protected_settings, storage, OverlappingSecretProvider())

    run = await manager.start_run("redact overlapping configured key occurrences")
    completed = await manager.wait(run.id)
    events = storage.get_events(run.id)
    persisted = json.dumps(
        [event.model_dump(mode="json") for event in events], ensure_ascii=False
    )

    assert completed.state is RunState.ANSWERED
    assert completed.turns[-1].summary == redact_text(content, api_key=configured)
    assert leak_fragment not in persisted
    for database_file in settings.data_dir.glob("test.db*"):
        assert leak_fragment.encode() not in database_file.read_bytes()


class _UnsafeReasoningStreamingProvider:
    supports_streaming = True

    def __init__(self, secret: str) -> None:
        self.secret = secret

    async def complete(self, *_args: object, **_kwargs: object) -> ModelResponse:
        raise AssertionError("stream_complete should be used")

    async def stream_complete(
        self,
        _messages: list[dict[str, object]],
        _tools: list[dict[str, object]] | None = None,
        *,
        on_delta,
        **_kwargs: object,
    ) -> ModelResponse:
        content = "The public response is safe and long enough to become durable before review."
        arguments = json.dumps({"content": content})
        await on_delta(
            ModelStreamDelta(
                tool_calls=[
                    ModelToolCallDelta(
                        index=0,
                        id="unsafe-reasoning",
                        name="respond_to_user",
                        arguments=arguments,
                        type="function",
                    )
                ]
            )
        )
        return ModelResponse(
            tool_calls=[
                ToolCall(
                    id="unsafe-reasoning",
                    name="respond_to_user",
                    arguments={"content": content},
                )
            ],
            reasoning_content=self.secret,
        )


@pytest.mark.asyncio
async def test_post_stream_private_reasoning_rejection_aborts_before_provider_completion(
    settings: Settings,
    storage: Storage,
) -> None:
    manager = AgentManager(
        settings,
        storage,
        _UnsafeReasoningStreamingProvider(settings.api_key),
    )

    run = await manager.start_run("reject unsafe private replay state")
    completed = await manager.wait(run.id)
    events = storage.get_events(run.id)
    event_types = [event.type for event in events]

    assert completed.state is RunState.FAILED
    assert EventType.ASSISTANT_OUTPUT_COMPLETED not in event_types
    assert EventType.ASSISTANT_OUTPUT_ABORTED in event_types
    assert storage.list_open_assistant_output_streams(run.id) == []


@pytest.mark.asyncio
async def test_answer_commit_fault_cannot_leave_a_terminal_run_with_an_open_turn(
    settings: Settings,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = "This streamed answer must not become a ghost terminal state after a DB fault."
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="answer-fault",
                        name="respond_to_user",
                        arguments={"content": content},
                    )
                ]
            )
        ],
        streaming=True,
    )
    original_commit = storage.commit_terminal_turn

    def fail_answer_once(*args, **kwargs):
        run = args[0]
        if run.state is RunState.ANSWERED:
            raise RuntimeError("injected answer commit fault")
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(storage, "commit_terminal_turn", fail_answer_once)
    manager = AgentManager(settings, storage, provider)

    run = await manager.start_run("exercise atomic answer failure")
    completed = await manager.wait(run.id)
    events = storage.get_events(run.id)

    assert completed.state is RunState.FAILED
    assert completed.turns[-1].outcome == "failed"
    assert not any(
        event.type is EventType.STATE_CHANGED
        and event.payload.get("state") == RunState.ANSWERED.value
        for event in events
    )
    assert any(event.type is EventType.ASSISTANT_OUTPUT_ABORTED for event in events)
    assert storage.list_open_assistant_output_streams(run.id) == []


@pytest.mark.asyncio
async def test_oversized_visible_tool_argument_is_aborted_before_schema_rejection(
    settings: Settings,
    storage: Storage,
) -> None:
    accepted = "A valid replacement answer is streamed after the oversized draft is rejected."
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="oversized",
                        name="respond_to_user",
                        arguments={"content": "x" * 20_001},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="accepted",
                        name="respond_to_user",
                        arguments={"content": accepted},
                    )
                ]
            ),
        ],
        streaming=True,
    )
    manager = AgentManager(settings, storage, provider)

    run = await manager.start_run("reject an oversized visible draft")
    completed = await manager.wait(run.id)
    events = storage.get_events(run.id)
    starts = [event for event in events if event.type is EventType.ASSISTANT_OUTPUT_STARTED]
    first_stream_id = str(starts[0].payload["stream_id"])
    first_deltas = [
        str(event.payload["delta"])
        for event in events
        if event.type is EventType.ASSISTANT_OUTPUT_DELTA
        and event.payload.get("stream_id") == first_stream_id
    ]
    first_abort = next(
        event
        for event in events
        if event.type is EventType.ASSISTANT_OUTPUT_ABORTED
        and event.payload.get("stream_id") == first_stream_id
    )

    assert completed.state is RunState.ANSWERED
    assert completed.turns[-1].summary == accepted
    assert len("".join(first_deltas)) <= 20_000
    assert first_abort.payload["reason"] == "visible_output_size_limit"


class _RetryingStreamingProvider:
    supports_streaming = True

    def __init__(self) -> None:
        self.attempts = 0

    async def complete(self, *_args: object, **_kwargs: object) -> ModelResponse:
        raise AssertionError("stream_complete should be used")

    async def stream_complete(
        self,
        _messages: list[dict[str, object]],
        _tools: list[dict[str, object]] | None = None,
        *,
        on_delta,
        **_kwargs: object,
    ) -> ModelResponse:
        self.attempts += 1
        text = (
            "first attempt partial output that is deliberately long enough to publish a batch"
            if self.attempts == 1
            else "second attempt is the accepted answer and is also long enough to stream safely"
        )
        arguments = json.dumps({"content": text})
        await on_delta(
            ModelStreamDelta(
                tool_calls=[
                    ModelToolCallDelta(
                        index=0,
                        id=f"answer-{self.attempts}",
                        name="respond_to_user",
                        arguments=arguments,
                        type="function",
                    )
                ]
            )
        )
        if self.attempts == 1:
            raise ProviderError("temporary disconnect", retryable=True, category="connection")
        return ModelResponse(
            tool_calls=[
                ToolCall(
                    id="answer-2",
                    name="respond_to_user",
                    arguments={"content": text},
                )
            ]
        )


class _ServerDirectedRetryProvider(_RetryingStreamingProvider):
    async def stream_complete(
        self,
        _messages: list[dict[str, object]],
        _tools: list[dict[str, object]] | None = None,
        *,
        on_delta,
        **_kwargs: object,
    ) -> ModelResponse:
        self.attempts += 1
        if self.attempts == 1:
            raise ProviderError(
                "server asked TraceForge to wait",
                retryable=True,
                category="rate_limit",
                retry_after_seconds=7.5,
            )
        text = "The second server-directed attempt returns a safe canonical answer."
        arguments = json.dumps({"content": text})
        await on_delta(
            ModelStreamDelta(
                tool_calls=[
                    ModelToolCallDelta(
                        index=0,
                        id="server-directed-answer",
                        name="respond_to_user",
                        arguments=arguments,
                        type="function",
                    )
                ]
            )
        )
        return ModelResponse(
            tool_calls=[
                ToolCall(
                    id="server-directed-answer",
                    name="respond_to_user",
                    arguments={"content": text},
                )
            ]
        )


@pytest.mark.asyncio
async def test_agent_waits_for_the_bounded_server_directed_retry_delay(
    settings: Settings,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_sleep = asyncio.sleep
    observed_delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        observed_delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(agent_module.asyncio, "sleep", record_sleep)
    manager = AgentManager(settings, storage, _ServerDirectedRetryProvider())

    run = await manager.start_run("respect server-directed retry pacing")
    completed = await manager.wait(run.id)
    retry = next(
        event for event in storage.get_events(run.id) if event.type is EventType.MODEL_RETRY
    )

    assert completed.state is RunState.ANSWERED
    assert observed_delays[0] == 7.5
    assert retry.payload["delay_seconds"] == 7.5


@pytest.mark.asyncio
async def test_retry_uses_distinct_streams_and_commits_only_the_successor(
    settings: Settings,
    storage: Storage,
) -> None:
    provider = _RetryingStreamingProvider()
    manager = AgentManager(
        replace(settings, model_retry_delay=0),
        storage,
        provider,
    )

    run = await manager.start_run("retry safely")
    completed = await manager.wait(run.id)
    events = storage.get_events(run.id)
    starts = [event for event in events if event.type is EventType.ASSISTANT_OUTPUT_STARTED]
    aborted = [event for event in events if event.type is EventType.ASSISTANT_OUTPUT_ABORTED]
    turn = next(event for event in events if event.type is EventType.TURN_COMPLETED)

    assert completed.state is RunState.ANSWERED
    assert provider.attempts == 2
    assert len(starts) == 2
    assert starts[0].payload["stream_id"] != starts[1].payload["stream_id"]
    assert aborted[0].payload["stream_id"] == starts[0].payload["stream_id"]
    assert aborted[0].payload["status"] == "retrying"
    assert turn.payload["final_stream_id"] == starts[1].payload["stream_id"]


@pytest.mark.asyncio
async def test_mixed_terminal_draft_is_discarded_before_a_fresh_canonical_stream(
    settings: Settings,
    storage: Storage,
) -> None:
    rejected = "This provisional response must never become the canonical answer. " * 3
    accepted = "This isolated successor response is the only canonical answer. " * 3
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="bad-answer",
                        name="respond_to_user",
                        arguments={"content": rejected},
                    ),
                    ToolCall(
                        id="mixed-read",
                        name="read_file",
                        arguments={"path": "README.md"},
                    ),
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="good-answer",
                        name="respond_to_user",
                        arguments={"content": accepted},
                    )
                ]
            ),
        ],
        streaming=True,
    )
    manager = AgentManager(settings, storage, provider)

    run = await manager.start_run("answer without a mixed tool batch")
    completed = await manager.wait(run.id)
    events = storage.get_events(run.id)
    starts = [event for event in events if event.type is EventType.ASSISTANT_OUTPUT_STARTED]
    aborted = [event for event in events if event.type is EventType.ASSISTANT_OUTPUT_ABORTED]
    turn = next(event for event in events if event.type is EventType.TURN_COMPLETED)

    assert completed.state is RunState.ANSWERED
    assert completed.turns[-1].summary == accepted
    assert len(starts) == 2
    assert aborted[0].payload["stream_id"] == starts[0].payload["stream_id"]
    assert aborted[0].payload["status"] == "discarded"
    assert turn.payload["final_stream_id"] == starts[1].payload["stream_id"]
    assert turn.payload["summary"] != rejected
    assert not any(
        event.type is EventType.TOOL_STARTED
        and event.payload.get("id") == "mixed-read"
        for event in events
    )


@pytest.mark.asyncio
async def test_verified_finish_summary_stream_is_committed_by_the_success_event(
    settings: Settings,
    storage: Storage,
) -> None:
    summary = (
        "   "
        + "The implementation summary streams provisionally and is committed after checks. " * 3
        + "   "
    )
    plan = {
        "summary": "Inspect and finish",
        "steps": [{"id": "inspect", "title": "Inspect the workspace"}],
        "acceptance_checks": [
            {"id": "review", "label": "Review is complete", "command": None}
        ],
        "risks": [],
    }
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="plan", name="submit_plan", arguments=plan)]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(id="finish", name="finish", arguments={"summary": summary})
                ]
            ),
        ],
        streaming=True,
    )
    manager = AgentManager(settings, storage, provider)

    run = await manager.start_run("inspect without changing files", verifier_enabled=False)
    completed = await manager.wait(run.id)
    events = storage.get_events(run.id)
    output_completed = next(
        event for event in events if event.type is EventType.ASSISTANT_OUTPUT_COMPLETED
    )
    turn = next(event for event in events if event.type is EventType.TURN_COMPLETED)

    assert completed.state is RunState.SUCCEEDED
    assert completed.turns[-1].summary == summary.strip()
    assert output_completed.payload["content"] == summary.strip()
    assert turn.payload["final_stream_id"] == output_completed.payload["stream_id"]
    assert not any(event.type is EventType.ASSISTANT_OUTPUT_ABORTED for event in events)


def _finish_stream_responses(summary: str) -> list[ModelResponse]:
    plan = {
        "summary": "Inspect and finish",
        "steps": [{"id": "inspect", "title": "Inspect the workspace"}],
        "acceptance_checks": [
            {"id": "review", "label": "Review is complete", "command": None}
        ],
        "risks": [],
    }
    return [
        ModelResponse(
            tool_calls=[ToolCall(id="plan", name="submit_plan", arguments=plan)]
        ),
        ModelResponse(
            tool_calls=[
                ToolCall(id="finish", name="finish", arguments={"summary": summary})
            ]
        ),
    ]


class _VerifierOutageAfterFinishProvider:
    supports_streaming = True

    def __init__(self, summary: str) -> None:
        self._delegate = ScriptedProvider(
            _finish_stream_responses(summary),
            streaming=True,
        )
        self._calls = 0

    async def complete(self, *_args: object, **_kwargs: object) -> ModelResponse:
        raise ProviderError(
            "verifier transport unavailable",
            retryable=True,
            category="connection",
        )

    async def stream_complete(self, *args, **kwargs) -> ModelResponse:
        self._calls += 1
        if self._calls <= 2:
            return await self._delegate.stream_complete(*args, **kwargs)
        raise AssertionError("unexpected extra public-output request")


@pytest.mark.asyncio
async def test_verifier_outage_aborts_the_provider_completed_finish_owner(
    settings: Settings,
    storage: Storage,
) -> None:
    summary = "A provisional finish remains visibly uncommitted until verification. " * 3
    manager = AgentManager(
        replace(settings, model_retry_delay=0),
        storage,
        _VerifierOutageAfterFinishProvider(summary),
    )

    run = await manager.start_run("interrupt during independent verification")
    interrupted = await manager.wait(run.id)
    events = storage.get_events(run.id)
    completed = next(
        event for event in events if event.type is EventType.ASSISTANT_OUTPUT_COMPLETED
    )
    abort = next(
        event
        for event in events
        if event.type is EventType.ASSISTANT_OUTPUT_ABORTED
        and event.payload.get("stream_id") == completed.payload["stream_id"]
    )

    assert interrupted.state is RunState.INTERRUPTED
    assert interrupted.interrupted_from is RunState.VERIFYING
    assert abort.payload["status"] == "interrupted"
    assert abort.payload["reason"] == "model_unavailable"
    assert storage.list_open_assistant_output_streams(run.id) == []
    assert storage.mark_all_active_runs_interrupted() == 0
    assert storage.list_open_assistant_output_streams(run.id) == []


class _BlockingVerifierAfterFinishProvider:
    supports_streaming = True

    def __init__(self, summary: str) -> None:
        self._delegate = ScriptedProvider(
            _finish_stream_responses(summary),
            streaming=True,
        )
        self._calls = 0
        self.verifier_started = asyncio.Event()

    async def complete(self, *_args: object, **_kwargs: object) -> ModelResponse:
        self.verifier_started.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled verifier request resumed")

    async def stream_complete(self, *args, **kwargs) -> ModelResponse:
        self._calls += 1
        if self._calls <= 2:
            return await self._delegate.stream_complete(*args, **kwargs)
        raise AssertionError("unexpected extra public-output request")


@pytest.mark.asyncio
async def test_shutdown_aborts_the_provider_completed_finish_owner(
    settings: Settings,
    storage: Storage,
) -> None:
    summary = "A provisional finish is durably closed when TraceForge shuts down. " * 3
    provider = _BlockingVerifierAfterFinishProvider(summary)
    manager = AgentManager(settings, storage, provider)

    run = await manager.start_run("shut down during independent verification")
    await asyncio.wait_for(provider.verifier_started.wait(), timeout=2)
    await manager.shutdown()

    interrupted = storage.get_run(run.id)
    events = storage.get_events(run.id)
    completed = next(
        event for event in events if event.type is EventType.ASSISTANT_OUTPUT_COMPLETED
    )
    abort = next(
        event
        for event in events
        if event.type is EventType.ASSISTANT_OUTPUT_ABORTED
        and event.payload.get("stream_id") == completed.payload["stream_id"]
    )

    assert interrupted.state is RunState.INTERRUPTED
    assert interrupted.interrupted_from is RunState.VERIFYING
    assert abort.payload["status"] == "interrupted"
    assert abort.payload["reason"] == "process_shutdown"
    assert storage.list_open_assistant_output_streams(run.id) == []


@pytest.mark.asyncio
async def test_manager_shutdown_closes_its_provider_exactly_once(
    settings: Settings,
    storage: Storage,
) -> None:
    class CloseCountingProvider(ScriptedProvider):
        def __init__(self) -> None:
            super().__init__([])
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    provider = CloseCountingProvider()
    manager = AgentManager(settings, storage, provider)

    await manager.shutdown()
    await manager.shutdown()

    assert provider.close_calls == 1


class _BlockingStreamingProvider:
    supports_streaming = True

    def __init__(self) -> None:
        self.published = asyncio.Event()

    async def complete(self, *_args: object, **_kwargs: object) -> ModelResponse:
        raise AssertionError("stream_complete should be used")

    async def stream_complete(
        self,
        _messages: list[dict[str, object]],
        _tools: list[dict[str, object]] | None = None,
        *,
        on_delta,
        **_kwargs: object,
    ) -> ModelResponse:
        content = (
            "A cancellable provisional response is long enough to publish before blocking. "
            * 2
        )
        await on_delta(
            ModelStreamDelta(
                tool_calls=[
                    ModelToolCallDelta(
                        index=0,
                        id="blocked-answer",
                        name="respond_to_user",
                        arguments=json.dumps({"content": content}),
                        type="function",
                    )
                ]
            )
        )
        self.published.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled stream resumed unexpectedly")


@pytest.mark.asyncio
async def test_cancelling_a_live_stream_persists_an_aborted_partial(
    settings: Settings,
    storage: Storage,
) -> None:
    provider = _BlockingStreamingProvider()
    manager = AgentManager(settings, storage, provider)

    run = await manager.start_run("start then cancel")
    await asyncio.wait_for(provider.published.wait(), timeout=2)
    cancelled = await manager.cancel(run.id)
    events = storage.get_events(run.id)
    aborted = next(
        event for event in events if event.type is EventType.ASSISTANT_OUTPUT_ABORTED
    )
    turn = next(event for event in events if event.type is EventType.TURN_COMPLETED)

    assert cancelled.state is RunState.CANCELLED
    assert aborted.payload["status"] == "cancelled"
    assert turn.payload["outcome"] == "cancelled"
    assert "final_stream_id" not in turn.payload
    assert not any(event.type is EventType.ASSISTANT_OUTPUT_COMPLETED for event in events)


def _seed_provider_completed_stream(
    storage: Storage,
    run_id: str,
    *,
    stream_id: str,
) -> None:
    base = {
        "turn_index": 1,
        "stream_id": stream_id,
        "phase": "verifying",
        "attempt": 1,
        "surface": "conversation",
        "provisional": True,
        "source_tool": "finish",
    }
    storage.append_event(
        run_id,
        EventType.ASSISTANT_OUTPUT_STARTED,
        {**base, "status": "streaming"},
    )
    storage.append_event(
        run_id,
        EventType.ASSISTANT_OUTPUT_DELTA,
        {**base, "segment_index": 1, "delta": "provisional summary"},
    )
    storage.append_event(
        run_id,
        EventType.ASSISTANT_OUTPUT_COMPLETED,
        {**base, "status": "provider_completed", "content": "provisional summary"},
    )


def test_process_restart_atomically_aborts_uncommitted_stream_generation(
    settings: Settings,
    storage: Storage,
) -> None:
    run = RunRecord(
        id="stream-restart",
        task="recover a partial stream",
        workspace=str(settings.workspace),
        state=RunState.PLANNING,
        turns=[ConversationTurn(index=1, request="recover a partial stream")],
    )
    storage.create_run(run)
    _seed_provider_completed_stream(storage, run.id, stream_id="restart-stream")

    assert storage.mark_active_runs_interrupted(settings.workspace) == 1

    events = storage.get_events(run.id)
    assert events[-2].type is EventType.ASSISTANT_OUTPUT_ABORTED
    assert events[-2].payload["status"] == "interrupted"
    assert events[-2].payload["reason"] == "process_restart"
    assert events[-1].type is EventType.STATE_CHANGED
    assert storage.get_run(run.id).state is RunState.INTERRUPTED
    assert storage.list_open_assistant_output_streams(run.id) == []


@pytest.mark.asyncio
async def test_cancelling_interrupted_provider_completed_stream_closes_draft_and_turn(
    settings: Settings,
    storage: Storage,
) -> None:
    run = RunRecord(
        id="cancel-provider-completed",
        task="cancel after verifier interruption",
        workspace=str(settings.workspace),
        state=RunState.INTERRUPTED,
        interrupted_from=RunState.VERIFYING,
        turns=[
            ConversationTurn(
                index=1,
                request="cancel after verifier interruption",
                summary="provisional summary",
                summary_stream_id="cancel-stream",
            )
        ],
    )
    storage.create_run(run)
    _seed_provider_completed_stream(storage, run.id, stream_id="cancel-stream")
    manager = AgentManager(settings, storage, ScriptedProvider([]))

    cancelled = await manager.cancel(run.id)
    events = storage.get_events(run.id)
    abort = next(event for event in events if event.type is EventType.ASSISTANT_OUTPUT_ABORTED)
    turn = next(event for event in events if event.type is EventType.TURN_COMPLETED)

    assert cancelled.state is RunState.CANCELLED
    assert cancelled.turns[-1].outcome == "cancelled"
    assert abort.seq < turn.seq
    assert abort.payload["status"] == "cancelled"
    assert storage.list_open_assistant_output_streams(run.id) == []


@pytest.mark.asyncio
async def test_rollback_of_interrupted_stream_aborts_draft_and_closes_active_turn(
    settings: Settings,
    storage: Storage,
) -> None:
    run = RunRecord(
        id="rollback-provider-completed",
        task="roll back after verifier interruption",
        workspace=str(settings.workspace),
        state=RunState.INTERRUPTED,
        interrupted_from=RunState.VERIFYING,
        turns=[
            ConversationTurn(
                index=1,
                request="roll back after verifier interruption",
                summary="provisional summary",
                summary_stream_id="rollback-stream",
            )
        ],
    )
    storage.create_run(run)
    _seed_provider_completed_stream(storage, run.id, stream_id="rollback-stream")
    manager = AgentManager(settings, storage, ScriptedProvider([]))

    await manager.rollback(run.id)
    rolled_back = storage.get_run(run.id)
    events = storage.get_events(run.id)
    abort = next(event for event in events if event.type is EventType.ASSISTANT_OUTPUT_ABORTED)
    turn = next(event for event in events if event.type is EventType.TURN_COMPLETED)
    rollback = next(event for event in events if event.type is EventType.ROLLBACK_COMPLETED)

    assert rolled_back.state is RunState.ROLLED_BACK
    assert rolled_back.turns[-1].outcome == "cancelled"
    assert abort.seq < turn.seq < rollback.seq
    assert abort.payload["reason"] == "run_rolled_back"
    assert storage.list_open_assistant_output_streams(run.id) == []


@pytest.mark.asyncio
async def test_cancel_and_rollback_are_linearized_without_reviving_stale_stream_state(
    settings: Settings,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = RunRecord(
        id="cancel-rollback-race",
        task="linearize cancel and rollback",
        workspace=str(settings.workspace),
        state=RunState.INTERRUPTED,
        interrupted_from=RunState.VERIFYING,
        turns=[
            ConversationTurn(
                index=1,
                request="linearize cancel and rollback",
                summary="provisional summary",
                summary_stream_id="racing-stream",
            )
        ],
    )
    storage.create_run(run)
    _seed_provider_completed_stream(storage, run.id, stream_id="racing-stream")
    manager = AgentManager(settings, storage, ScriptedProvider([]))
    cancel_entered = asyncio.Event()
    allow_cancel = asyncio.Event()

    async def delayed_tool_cancel(_run_id: str) -> None:
        cancel_entered.set()
        await allow_cancel.wait()

    monkeypatch.setattr(manager.tools, "cancel", delayed_tool_cancel)
    cancel_task = asyncio.create_task(manager.cancel(run.id))
    await asyncio.wait_for(cancel_entered.wait(), timeout=2)
    rollback_task = asyncio.create_task(manager.rollback(run.id))
    await asyncio.sleep(0.05)
    allow_cancel.set()

    await asyncio.gather(cancel_task, rollback_task)
    final = storage.get_run(run.id)
    events = storage.get_events(run.id)

    assert final.state is RunState.ROLLED_BACK
    assert final.turns[-1].outcome == "cancelled"
    assert sum(event.type is EventType.TURN_COMPLETED for event in events) == 1
    assert sum(event.type is EventType.RUN_COMPLETED for event in events) == 1
    assert sum(event.type is EventType.ROLLBACK_COMPLETED for event in events) == 1
    assert storage.list_open_assistant_output_streams(run.id) == []

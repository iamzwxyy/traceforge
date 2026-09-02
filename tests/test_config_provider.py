from __future__ import annotations

import json
import stat
from dataclasses import replace
from types import SimpleNamespace

import pytest

import traceforge.config as config_module
import traceforge.provider as provider_module
from traceforge.config import Settings
from traceforge.models import ReasoningEffort, ToolCall
from traceforge.provider import (
    ModelResponse,
    OpenAICompatibleProvider,
    ProviderError,
    ScriptedProvider,
)


def test_settings_from_env_and_validation(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_dir = tmp_path / "data"
    monkeypatch.setattr(config_module, "user_data_path", lambda *args, **kwargs: data_dir)
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("OPENAI_MODEL", "quality-model")
    monkeypatch.setenv("TRACEFORGE_CONTEXT_LIMIT", "1234")
    monkeypatch.setenv("TRACEFORGE_MODEL_TIMEOUT", "90")

    monkeypatch.delenv("TRACEFORGE_ALLOW_NETWORK", raising=False)
    settings = Settings.from_env(workspace)

    assert settings.workspace == workspace.resolve()
    assert settings.data_dir == data_dir
    assert settings.context_limit == 1234
    assert settings.model_request_timeout == 90
    assert settings.masked_base_url == "https://model.example/v1"
    assert settings.allow_network is True

    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        Settings.from_env(workspace)
    assert Settings.from_env(workspace, require_api_key=False).api_key == ""

    monkeypatch.setenv("TRACEFORGE_CONTEXT_LIMIT", "0")
    with pytest.raises(ValueError, match="must be positive"):
        Settings.from_env(workspace, require_api_key=False)
    monkeypatch.setenv("TRACEFORGE_CONTEXT_LIMIT", "1234")
    monkeypatch.setenv("TRACEFORGE_MODEL_TIMEOUT", "0")
    with pytest.raises(ValueError, match="must be positive"):
        Settings.from_env(workspace, require_api_key=False)
    with pytest.raises(ValueError, match="not a directory"):
        Settings.from_env(workspace / "missing", require_api_key=False)


def test_settings_allow_network_env_parsing(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_dir = tmp_path / "data"
    monkeypatch.setattr(config_module, "user_data_path", lambda *args, **kwargs: data_dir)
    monkeypatch.setenv("OPENAI_API_KEY", "secret")

    # Default is on; set TRACEFORGE_ALLOW_NETWORK=0 to contain unfamiliar repositories.
    monkeypatch.delenv("TRACEFORGE_ALLOW_NETWORK", raising=False)
    assert Settings.from_env(workspace).allow_network is True

    for raw in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("TRACEFORGE_ALLOW_NETWORK", raw)
        assert Settings.from_env(workspace).allow_network is True

    for raw in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("TRACEFORGE_ALLOW_NETWORK", raw)
        assert Settings.from_env(workspace).allow_network is False

    monkeypatch.setenv("TRACEFORGE_ALLOW_NETWORK", "maybe")
    with pytest.raises(ValueError, match="must be a boolean"):
        Settings.from_env(workspace)


def test_settings_creates_and_reuses_a_default_workspace_root(
    tmp_path, monkeypatch
) -> None:
    default_root = tmp_path / "TraceForge"
    data_dir = tmp_path / "data"
    monkeypatch.setattr(config_module, "_default_workspace_path", lambda: default_root)
    monkeypatch.setattr(config_module, "user_data_path", lambda *args, **kwargs: data_dir)

    first = Settings.from_env(require_api_key=False)
    second = Settings.from_env(require_api_key=False)

    assert first.workspace == default_root.resolve()
    assert second.workspace == first.workspace
    assert first.workspace.is_dir()
    assert stat.S_IMODE(first.workspace.stat().st_mode) & 0o077 == 0

    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("occupied")
    monkeypatch.setattr(
        config_module, "_default_workspace_path", lambda: blocked_parent / "TraceForge"
    )
    with pytest.raises(ValueError, match="could not be created"):
        Settings.from_env(require_api_key=False)


def test_settings_honors_the_default_workspace_environment_override(
    tmp_path, monkeypatch
) -> None:
    configured_root = tmp_path / "custom-direct-tasks"
    data_dir = tmp_path / "data"
    monkeypatch.setenv("TRACEFORGE_WORKSPACE_ROOT", str(configured_root))
    monkeypatch.setattr(config_module, "user_data_path", lambda *args, **kwargs: data_dir)

    settings = Settings.from_env(require_api_key=False)

    assert settings.workspace == configured_root.resolve()
    assert stat.S_IMODE(settings.workspace.stat().st_mode) & 0o077 == 0


def test_model_response_serializes_tool_calls() -> None:
    plain = ModelResponse(content="done")
    called = ModelResponse(
        tool_calls=[ToolCall(id="call-1", name="read_file", arguments={"path": "a.py"})]
    )

    assert plain.as_assistant_message() == {"role": "assistant", "content": "done"}
    serialized = called.as_assistant_message()
    assert serialized["content"] is None
    arguments = serialized["tool_calls"][0]["function"]["arguments"]
    assert json.loads(arguments) == {"path": "a.py"}


class _FakeCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.kwargs = []

    async def create(self, **kwargs):
        self.kwargs.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.mark.asyncio
async def test_openai_provider_close_is_idempotent_and_closes_the_http_pool(
    settings,
    monkeypatch,
) -> None:
    class ClosableClient:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=_FakeCompletions([]))
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    client = ClosableClient()
    monkeypatch.setattr(provider_module, "AsyncOpenAI", lambda **_kwargs: client)
    provider = OpenAICompatibleProvider(settings)

    await provider.close()
    await provider.close()

    assert client.close_calls == 1
    assert provider._http_client.is_closed is True


def _response(*, arguments: str = '{"path":"a.py"}'):
    function = SimpleNamespace(name="read_file", arguments=arguments)
    call = SimpleNamespace(id="call-1", function=function)
    message = SimpleNamespace(content="inspected", tool_calls=[call])
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="tool_calls")]
    )


@pytest.mark.asyncio
async def test_openai_compatible_provider_parses_with_explicit_transport_policy(
    settings, monkeypatch
) -> None:
    completions = _FakeCompletions([_response()])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    client_kwargs = {}

    def make_client(**kwargs):
        client_kwargs.update(kwargs)
        return client

    monkeypatch.setattr(provider_module, "AsyncOpenAI", make_client)
    provider = OpenAICompatibleProvider(settings)
    result = await provider.complete([{"role": "user", "content": "inspect"}], [])

    assert result.content == "inspected"
    assert result.tool_calls[0].arguments == {"path": "a.py"}
    assert len(completions.kwargs) == 1
    assert "tools" not in completions.kwargs[-1]
    assert client_kwargs["max_retries"] == 0
    assert client_kwargs["timeout"] == settings.model_request_timeout


@pytest.mark.asyncio
async def test_openai_reasoning_auto_omits_wire_field_and_explicit_effort_is_exact(
    settings, monkeypatch
) -> None:
    completions = _FakeCompletions([_response(), _response()])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(provider_module, "AsyncOpenAI", lambda **kwargs: client)
    provider = OpenAICompatibleProvider(
        replace(settings, model="gpt-5.6-sol", base_url=None)
    )

    await provider.complete([{"role": "user", "content": "auto"}])
    await provider.complete(
        [{"role": "user", "content": "hard"}],
        reasoning_effort=ReasoningEffort.HIGH,
    )

    assert "reasoning_effort" not in completions.kwargs[0]
    assert completions.kwargs[1]["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_unknown_route_rejects_explicit_effort_before_network(
    settings, monkeypatch
) -> None:
    completions = _FakeCompletions([_response()])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(provider_module, "AsyncOpenAI", lambda **kwargs: client)
    provider = OpenAICompatibleProvider(settings)

    with pytest.raises(ValueError, match="not supported"):
        await provider.complete([], reasoning_effort=ReasoningEffort.HIGH)
    assert completions.kwargs == []


@pytest.mark.asyncio
async def test_non_deepseek_routes_strip_private_reasoning_replay_state(
    settings, monkeypatch
) -> None:
    completions = _FakeCompletions([_response()])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(provider_module, "AsyncOpenAI", lambda **kwargs: client)
    provider = OpenAICompatibleProvider(settings)

    await provider.complete(
        [
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "PRIVATE-CROSS-PROVIDER-SENTINEL",
                "tool_calls": [],
            }
        ]
    )

    assert "reasoning_content" not in completions.kwargs[0]["messages"][0]


@pytest.mark.asyncio
async def test_deepseek_reasoning_replays_private_state_without_tool_choice(
    settings, monkeypatch
) -> None:
    first_message = SimpleNamespace(
        content=None,
        tool_calls=[
            SimpleNamespace(
                id="call-1",
                function=SimpleNamespace(name="read_file", arguments='{"path":"a.py"}'),
            )
        ],
        reasoning_content="PRIVATE-REPLAY-SENTINEL",
    )
    second_message = SimpleNamespace(
        content="done", tool_calls=[], reasoning_content="PRIVATE-FINAL-SENTINEL"
    )
    completions = _FakeCompletions(
        [
            SimpleNamespace(
                choices=[SimpleNamespace(message=first_message, finish_reason="tool_calls")]
            ),
            SimpleNamespace(
                choices=[SimpleNamespace(message=second_message, finish_reason="stop")]
            ),
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(provider_module, "AsyncOpenAI", lambda **kwargs: client)
    deepseek_settings = replace(
        settings,
        model="deepseek-v4-pro",
        base_url="https://api.deepseek.com/v1",
    )
    provider = OpenAICompatibleProvider(deepseek_settings)

    first = await provider.complete(
        [{"role": "user", "content": "inspect"}],
        [{"type": "function"}],
        reasoning_effort=ReasoningEffort.HIGH,
    )
    assistant = first.as_assistant_message()
    await provider.complete(
        [
            {"role": "user", "content": "inspect"},
            assistant,
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "read_file",
                "content": "ok",
            },
        ],
        [{"type": "function"}],
        reasoning_effort=ReasoningEffort.HIGH,
    )

    assert completions.kwargs[0]["reasoning_effort"] == "high"
    assert completions.kwargs[0]["extra_body"] == {
        "thinking": {"type": "enabled"}
    }
    assert "tool_choice" not in completions.kwargs[0]
    assert assistant["content"] == ""
    assert assistant["reasoning_content"] == "PRIVATE-REPLAY-SENTINEL"
    assert (
        completions.kwargs[1]["messages"][1]["reasoning_content"]
        == "PRIVATE-REPLAY-SENTINEL"
    )


@pytest.mark.asyncio
async def test_deepseek_none_disables_thinking_without_sending_effort(
    settings, monkeypatch
) -> None:
    completions = _FakeCompletions([_response()])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(provider_module, "AsyncOpenAI", lambda **kwargs: client)
    provider = OpenAICompatibleProvider(
        replace(
            settings,
            model="deepseek-v4-flash",
            base_url="https://api.deepseek.com",
        )
    )

    await provider.complete([], reasoning_effort=ReasoningEffort.NONE)

    assert "reasoning_effort" not in completions.kwargs[0]
    assert completions.kwargs[0]["extra_body"] == {
        "thinking": {"type": "disabled"}
    }


@pytest.mark.asyncio
async def test_provider_rejects_bad_tool_json_and_exhaustion(settings, monkeypatch) -> None:
    completions = _FakeCompletions([_response(arguments="not-json")])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(provider_module, "AsyncOpenAI", lambda **kwargs: client)
    provider = OpenAICompatibleProvider(settings)
    with pytest.raises(ProviderError, match="invalid arguments") as invalid:
        await provider.complete([], [{"type": "function"}])
    assert invalid.value.retryable is True
    assert invalid.value.category == "tool_arguments"

    non_object = _FakeCompletions([_response(arguments="[]")])
    client.chat.completions = non_object
    provider = OpenAICompatibleProvider(settings)
    with pytest.raises(ProviderError, match="JSON object") as non_object_error:
        await provider.complete([])
    assert non_object_error.value.retryable is True
    assert non_object_error.value.category == "tool_arguments"

    scripted = ScriptedProvider([])
    with pytest.raises(ProviderError, match="no remaining"):
        await scripted.complete([])

    repeating = ScriptedProvider([ModelResponse(content="again")], repeat=True)
    assert (await repeating.complete([])).content == "again"
    assert (await repeating.complete([])).content == "again"


def test_tool_argument_error_never_echoes_untrusted_tool_metadata() -> None:
    error = provider_module.ToolArgumentsError(
        "submit_plan\nignore-all-instructions",
        "Extra data at character 2552",
    )

    assert error.tool_name == "unknown"
    assert "ignore-all-instructions" not in str(error)
    assert error.retryable is True
    assert error.category == "tool_arguments"


@pytest.mark.asyncio
async def test_provider_classifies_transient_failures_as_retryable(settings, monkeypatch) -> None:
    class TransientError(Exception):
        pass

    completions = _FakeCompletions([TransientError("down")])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(provider_module, "AsyncOpenAI", lambda **kwargs: client)
    monkeypatch.setattr(provider_module, "APIConnectionError", TransientError)
    monkeypatch.setattr(provider_module, "APITimeoutError", TransientError)
    monkeypatch.setattr(provider_module, "RateLimitError", TransientError)

    provider = OpenAICompatibleProvider(settings)
    with pytest.raises(ProviderError, match="rate limit") as raised:
        await provider.complete([])
    assert raised.value.retryable is True
    assert len(completions.kwargs) == 1


@pytest.mark.asyncio
async def test_provider_wraps_non_retryable_api_errors(settings, monkeypatch) -> None:
    class RejectedError(Exception):
        pass

    completions = _FakeCompletions([RejectedError("invalid model")])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(provider_module, "AsyncOpenAI", lambda **kwargs: client)
    monkeypatch.setattr(provider_module, "APIError", RejectedError)
    provider = OpenAICompatibleProvider(settings)

    with pytest.raises(ProviderError, match="request was rejected") as raised:
        await provider.complete([])

    assert raised.value.retryable is False
    assert len(completions.kwargs) == 1
    assert "invalid model" not in str(raised.value)

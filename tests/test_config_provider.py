from __future__ import annotations

import stat
from types import SimpleNamespace

import pytest

import traceforge.config as config_module
import traceforge.provider as provider_module
from traceforge.config import Settings
from traceforge.models import ToolCall
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

    settings = Settings.from_env(workspace)

    assert settings.workspace == workspace.resolve()
    assert settings.data_dir == data_dir
    assert settings.context_limit == 1234
    assert settings.model_request_timeout == 90
    assert settings.masked_base_url == "https://model.example/v1"

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
    assert serialized["tool_calls"][0]["function"]["arguments"] == '{"path": "a.py"}'


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
async def test_provider_rejects_bad_tool_json_and_exhaustion(settings, monkeypatch) -> None:
    completions = _FakeCompletions([_response(arguments="not-json")])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(provider_module, "AsyncOpenAI", lambda **kwargs: client)
    provider = OpenAICompatibleProvider(settings)
    with pytest.raises(ProviderError, match="invalid JSON"):
        await provider.complete([], [{"type": "function"}])

    non_object = _FakeCompletions([_response(arguments="[]")])
    client.chat.completions = non_object
    provider = OpenAICompatibleProvider(settings)
    with pytest.raises(ProviderError, match="JSON object"):
        await provider.complete([])

    scripted = ScriptedProvider([])
    with pytest.raises(ProviderError, match="no remaining"):
        await scripted.complete([])

    repeating = ScriptedProvider([ModelResponse(content="again")], repeat=True)
    assert (await repeating.complete([])).content == "again"
    assert (await repeating.complete([])).content == "again"


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

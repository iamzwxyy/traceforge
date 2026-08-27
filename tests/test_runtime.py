from __future__ import annotations

from pathlib import Path

import pytest

import traceforge.runtime as runtime_module
from traceforge.events import EventBroker
from traceforge.models import ProviderConfig, RunRecord, RunState, ToolCall
from traceforge.provider import ModelResponse
from traceforge.runtime import AgentRuntime, validate_credential_file
from traceforge.storage import Storage


def test_validate_credential_file_requires_owner_only_permissions(tmp_path: Path) -> None:
    credential = tmp_path / "key"
    credential.write_text("probe\n")
    credential.chmod(0o644)

    with pytest.raises(ValueError, match="chmod 600"):
        validate_credential_file(credential)

    credential.chmod(0o600)
    assert validate_credential_file(credential) == credential.resolve()


def test_credential_readiness_rejects_empty_or_multiline_files(
    settings, storage: Storage, tmp_path: Path
) -> None:
    credential = tmp_path / "key"
    runtime = AgentRuntime(settings, storage, EventBroker(storage))
    config = ProviderConfig(model="model", credential_file=str(credential))

    credential.write_text("")
    credential.chmod(0o600)
    assert runtime.credential_configured(config) is False
    credential.write_text("first\nsecond\n")
    assert runtime.credential_configured(config) is False
    credential.write_text("one-line-value\n")
    assert runtime.credential_configured(config) is True


@pytest.mark.asyncio
async def test_connection_probe_requires_native_tool_call(
    settings, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ProbeProvider:
        def __init__(self, _settings) -> None:
            pass

        async def complete(self, messages, tools=None) -> ModelResponse:
            assert messages and tools
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="probe",
                        name="report_connection",
                        arguments={"status": "ok"},
                    )
                ]
            )

    monkeypatch.setattr(runtime_module, "OpenAICompatibleProvider", ProbeProvider)
    runtime = AgentRuntime(settings, storage, EventBroker(storage))
    storage.save_provider_config(
        ProviderConfig(model="tool-model", base_url="https://provider.example/v1")
    )

    result = await runtime.test_connection()

    assert result["ok"] is True
    assert result["model"] == "tool-model"
    assert "tool calling verified" in result["detail"]


def test_workspace_manager_resolves_credential_file_without_persisting_value(
    settings, storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential = tmp_path / "provider.key"
    credential.write_text("file-only-test-value\n")
    credential.chmod(0o600)
    captured: dict[str, str | None] = {}

    class CapturingProvider:
        def __init__(self, provider_settings) -> None:
            captured["api_key"] = provider_settings.api_key

    monkeypatch.setattr(runtime_module, "OpenAICompatibleProvider", CapturingProvider)
    storage.save_provider_config(
        ProviderConfig(
            model="file-backed-model",
            base_url="https://provider.example/v1",
            credential_file=str(credential),
        )
    )
    runtime = AgentRuntime(settings, storage, EventBroker(storage))

    runtime.manager_for_workspace(settings.workspace)

    assert captured["api_key"] == "file-only-test-value"
    saved = storage.get_provider_config(ProviderConfig(model="fallback"))
    assert saved.credential_file == str(credential)
    assert "file-only-test-value" not in saved.model_dump_json()


@pytest.mark.asyncio
async def test_provider_config_can_change_while_runs_are_interrupted(
    settings, storage: Storage
) -> None:
    storage.create_run(
        RunRecord(
            id="paused",
            task="Resume with a repaired provider",
            workspace=str(settings.workspace),
            state=RunState.INTERRUPTED,
            interrupted_from=RunState.EXECUTING,
        )
    )
    runtime = AgentRuntime(settings, storage, EventBroker(storage))

    saved = await runtime.save_provider_config(
        ProviderConfig(model="repaired-model", base_url="https://provider.example/v1")
    )

    assert saved.model == "repaired-model"
    assert storage.get_run("paused").state is RunState.INTERRUPTED

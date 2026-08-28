from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

import traceforge.runtime as runtime_module
from traceforge.agent import RunConflictError
from traceforge.events import EventBroker
from traceforge.models import ProviderConfig, RunRecord, RunState, ToolCall, utc_now
from traceforge.provider import ModelResponse, ProviderError, ScriptedProvider
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


def test_environment_credential_must_also_be_exactly_one_line(
    settings, storage: Storage
) -> None:
    multiline = "\n".join(("first", "second"))
    invalid = replace(settings, api_key=multiline)
    runtime = AgentRuntime(invalid, storage, EventBroker(storage))

    assert runtime.credential_configured() is False


@pytest.mark.asyncio
async def test_connection_probe_requires_native_tool_call(
    settings, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []

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

        async def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(runtime_module, "OpenAICompatibleProvider", ProbeProvider)
    runtime = AgentRuntime(settings, storage, EventBroker(storage))
    storage.save_provider_config(
        ProviderConfig(model="tool-model", base_url="https://provider.example/v1")
    )

    result = await runtime.test_connection()

    assert result["ok"] is True
    assert result["model"] == "tool-model"
    assert "tool calling verified" in result["detail"]
    assert storage.get_provider_verified_at() is not None
    assert runtime.connection_verified() is True
    assert closed == [True]


@pytest.mark.asyncio
async def test_connection_probe_redaction_cannot_collide_with_configured_key(
    settings, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = "[REDACTED]"

    class FailingProbeProvider:
        def __init__(self, _settings) -> None:
            pass

        async def complete(self, _messages, _tools=None) -> ModelResponse:
            raise ProviderError(f"credential={configured}", category="authentication")

        async def close(self) -> None:
            return None

    monkeypatch.setattr(runtime_module, "OpenAICompatibleProvider", FailingProbeProvider)
    runtime = AgentRuntime(settings, storage, EventBroker(storage))

    result = await runtime._probe_connection(
        ProviderConfig(model="tool-model", base_url="https://provider.example/v1"),
        configured,
    )

    assert result["ok"] is False
    assert configured not in result["detail"]


@pytest.mark.asyncio
async def test_failed_draft_probe_keeps_verified_config_and_never_writes_new_key(
    settings, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_secret = "verified-old-secret"
    rejected_secret = "rejected-new-secret"

    class ConditionalProbeProvider:
        def __init__(self, provider_settings) -> None:
            self.api_key = provider_settings.api_key

        async def complete(self, messages, tools=None) -> ModelResponse:
            assert messages and tools
            if self.api_key == rejected_secret:
                raise ProviderError(f"Rejected credential {rejected_secret}")
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="probe",
                        name="report_connection",
                        arguments={"status": "ok"},
                    )
                ]
            )

    monkeypatch.setattr(
        runtime_module, "OpenAICompatibleProvider", ConditionalProbeProvider
    )
    runtime = AgentRuntime(settings, storage, EventBroker(storage))
    first = await runtime.test_and_save_provider_config(
        ProviderConfig(model="verified-model", base_url="https://verified.example/v1"),
        api_key=old_secret,
    )
    assert first["ok"] is True
    saved_before = runtime.provider_config
    verified_before = storage.get_provider_verified_at()
    credential_before = Path(saved_before.credential_file or "")
    managed_directory = settings.data_dir / "provider-credentials"
    managed_before = set(managed_directory.glob("provider-credential-*.key"))

    failed = await runtime.test_and_save_provider_config(
        ProviderConfig(model="rejected-model", base_url="https://rejected.example/v1"),
        api_key=rejected_secret,
    )

    assert failed["ok"] is False
    assert rejected_secret not in failed["detail"]
    assert runtime.provider_config == saved_before
    assert storage.get_provider_verified_at() == verified_before
    assert runtime.connection_verified() is True
    credential_contents = await asyncio.to_thread(
        credential_before.read_text, encoding="utf-8"
    )
    assert credential_contents == f"{old_secret}\n"
    assert set(managed_directory.glob("provider-credential-*.key")) == managed_before
    for database_file in settings.data_dir.glob("test.db*"):
        database_contents = await asyncio.to_thread(database_file.read_bytes)
        assert rejected_secret.encode() not in database_contents


@pytest.mark.asyncio
async def test_failed_probe_of_saved_config_revokes_its_verification(
    settings, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    reject_probe = False

    class ConditionalProbeProvider:
        def __init__(self, _settings) -> None:
            pass

        async def complete(self, messages, tools=None) -> ModelResponse:
            assert messages and tools
            if reject_probe:
                raise ProviderError("The saved route stopped accepting requests")
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="probe",
                        name="report_connection",
                        arguments={"status": "ok"},
                    )
                ]
            )

    monkeypatch.setattr(
        runtime_module, "OpenAICompatibleProvider", ConditionalProbeProvider
    )
    runtime = AgentRuntime(settings, storage, EventBroker(storage))
    config = ProviderConfig(
        model="saved-model", base_url="https://saved.example/v1"
    )
    assert (await runtime.test_and_save_provider_config(config))["ok"] is True
    assert runtime.connection_verified() is True

    reject_probe = True
    assert (await runtime.test_and_save_provider_config(runtime.provider_config))["ok"] is False
    assert storage.get_provider_verified_at() is None
    assert runtime.connection_verified() is False

    reject_probe = False
    assert (await runtime.test_connection())["ok"] is True
    assert runtime.connection_verified() is True

    reject_probe = True
    assert (await runtime.test_connection())["ok"] is False
    assert storage.get_provider_verified_at() is None
    assert runtime.connection_verified() is False


@pytest.mark.asyncio
async def test_start_waits_for_atomic_provider_probe_and_uses_the_saved_route(
    settings, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()

    class BlockingProbeProvider:
        def __init__(self, provider_settings) -> None:
            self.model = provider_settings.model

        async def complete(self, messages, tools=None) -> ModelResponse:
            tool_names = {
                tool["function"]["name"]
                for tool in tools or []
                if "function" in tool
            }
            if "report_connection" in tool_names:
                probe_started.set()
                await release_probe.wait()
                return ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="probe",
                            name="report_connection",
                            arguments={"status": "ok"},
                        )
                    ]
                )
            await asyncio.Future()
            raise AssertionError("unreachable")

    monkeypatch.setattr(
        runtime_module, "OpenAICompatibleProvider", BlockingProbeProvider
    )
    storage.save_provider_config(
        ProviderConfig(model="old-verified-model"), verified_at=utc_now()
    )
    runtime = AgentRuntime(settings, storage, EventBroker(storage))
    probe_task = asyncio.create_task(
        runtime.test_and_save_provider_config(ProviderConfig(model="new-verified-model"))
    )
    await probe_started.wait()
    start_task = asyncio.create_task(
        runtime.start_run("Use the newly verified route", settings.workspace)
    )
    await asyncio.sleep(0)

    assert start_task.done() is False
    assert storage.list_runs() == []

    release_probe.set()
    try:
        probe_result, run = await asyncio.gather(probe_task, start_task)
        assert probe_result["ok"] is True
        assert runtime.provider_config.model == "new-verified-model"
        assert runtime.connection_verified() is True
        manager = runtime.manager_for_run(run.id)
        assert manager.settings.model == "new-verified-model"
        assert manager.provider.model == "new-verified-model"  # type: ignore[attr-defined]
    finally:
        release_probe.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_ordinary_provider_save_clears_a_successful_verification(
    settings, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ProbeProvider:
        def __init__(self, _settings) -> None:
            pass

        async def complete(self, messages, tools=None) -> ModelResponse:
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
    tested = await runtime.test_and_save_provider_config(
        ProviderConfig(model="verified-model", base_url="https://verified.example/v1")
    )
    assert tested["ok"] is True
    assert runtime.connection_verified() is True

    await runtime.save_provider_config(runtime.provider_config)

    assert storage.get_provider_verified_at() is None
    assert runtime.connection_verified() is False


@pytest.mark.asyncio
async def test_successful_probe_rolls_back_staged_key_when_config_commit_fails(
    settings, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ProbeProvider:
        def __init__(self, _settings) -> None:
            pass

        async def complete(self, messages, tools=None) -> ModelResponse:
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
    await runtime.test_and_save_provider_config(
        ProviderConfig(model="old-model"), api_key="test-old-atomic-secret"
    )
    saved_before = runtime.provider_config
    verified_before = storage.get_provider_verified_at()
    managed_directory = settings.data_dir / "provider-credentials"
    files_before = set(managed_directory.glob("provider-credential-*.key"))

    def fail_commit(*_args, **_kwargs) -> None:
        raise RuntimeError("database write failed")

    monkeypatch.setattr(storage, "save_provider_config", fail_commit)
    with pytest.raises(RuntimeError, match="database write failed"):
        await runtime.test_and_save_provider_config(
            ProviderConfig(model="new-model"), api_key="test-new-atomic-secret"
        )

    assert runtime.provider_config == saved_before
    assert storage.get_provider_verified_at() == verified_before
    assert set(managed_directory.glob("provider-credential-*.key")) == files_before


@pytest.mark.asyncio
async def test_replacing_a_manual_root_credential_never_deletes_it(
    settings, storage: Storage
) -> None:
    manual_credential = settings.data_dir / "provider-credential-user.key"
    manual_credential.write_text("user-owned-secret\n", encoding="utf-8")
    manual_credential.chmod(0o600)
    storage.save_provider_config(
        ProviderConfig(
            model="manual-model",
            credential_file=str(manual_credential),
        ),
        verified_at=utc_now(),
    )
    runtime = AgentRuntime(settings, storage, EventBroker(storage))

    saved = await runtime.save_provider_config(
        ProviderConfig(model="managed-model"),
        api_key="test-replacement-secret",
    )

    assert manual_credential.read_text(encoding="utf-8") == "user-owned-secret\n"
    assert Path(saved.credential_file or "").parent == (
        settings.data_dir / "provider-credentials"
    ).resolve()


@pytest.mark.asyncio
async def test_replacing_the_exact_legacy_managed_credential_removes_it(
    settings, storage: Storage
) -> None:
    legacy_credential = settings.data_dir / "provider-credential.key"
    legacy_credential.write_text("legacy-managed-secret\n", encoding="utf-8")
    legacy_credential.chmod(0o600)
    storage.save_provider_config(
        ProviderConfig(
            model="legacy-model",
            credential_file=str(legacy_credential),
        )
    )
    runtime = AgentRuntime(settings, storage, EventBroker(storage))

    await runtime.save_provider_config(
        ProviderConfig(model="replacement-model"),
        api_key="test-replacement-secret",
    )

    assert legacy_credential.exists() is False


@pytest.mark.asyncio
async def test_managed_credential_directory_symlink_is_rejected_without_write_through(
    settings, storage: Storage, tmp_path: Path
) -> None:
    outside_directory = tmp_path / "outside-managed-credentials"
    outside_directory.mkdir()
    managed_directory = settings.data_dir / "provider-credentials"
    managed_directory.symlink_to(outside_directory, target_is_directory=True)
    runtime = AgentRuntime(settings, storage, EventBroker(storage))

    with pytest.raises(ValueError, match="must not be a symbolic link"):
        await runtime.save_provider_config(
            ProviderConfig(model="must-not-save"),
            api_key="test-must-not-write-through",
        )

    assert list(outside_directory.iterdir()) == []
    assert storage.get_provider_config(ProviderConfig(model="fallback")).model == "fallback"
    for database_file in settings.data_dir.glob("test.db*"):
        database_contents = await asyncio.to_thread(database_file.read_bytes)
        assert b"test-must-not-write-through" not in database_contents


@pytest.mark.asyncio
async def test_resumed_run_blocks_provider_changes_before_worker_leaves_interrupted(
    settings, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage.create_run(
        RunRecord(
            id="resume-window",
            task="Resume without exposing a provider swap window",
            workspace=str(settings.workspace),
            state=RunState.INTERRUPTED,
            interrupted_from=RunState.PLANNING,
        )
    )
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="answer",
                        name="respond_to_user",
                        arguments={"content": "Resumed safely."},
                    )
                ]
            )
        ]
    )
    runtime = AgentRuntime(
        settings,
        storage,
        EventBroker(storage),
        provider_override=provider,
    )
    manager = runtime.manager_for_run("resume-window")
    worker_entered = asyncio.Event()
    release_worker = asyncio.Event()
    prepare_resume = manager._prepare_resume

    async def blocked_prepare_resume(run: RunRecord) -> None:
        worker_entered.set()
        await release_worker.wait()
        await prepare_resume(run)

    monkeypatch.setattr(manager, "_prepare_resume", blocked_prepare_resume)

    resumed = await runtime.resume_run("resume-window")
    assert resumed.state is RunState.INTERRUPTED
    await worker_entered.wait()
    assert storage.get_run("resume-window").state is RunState.INTERRUPTED

    with pytest.raises(RunConflictError, match="before changing model settings"):
        await runtime.save_provider_config(ProviderConfig(model="blocked-save"))
    with pytest.raises(RunConflictError, match="before changing model settings"):
        await runtime.test_and_save_provider_config(
            ProviderConfig(model="blocked-test-and-save")
        )

    release_worker.set()
    completed = await manager.wait("resume-window")
    assert completed.state.terminal is True
    await asyncio.gather(*tuple(runtime._resume_watchers))
    assert runtime._resumed_runs == set()

    saved = await runtime.save_provider_config(ProviderConfig(model="allowed-save"))
    tested = await runtime.test_and_save_provider_config(
        ProviderConfig(model="allowed-tested-save")
    )
    assert saved.model == "allowed-save"
    assert tested["ok"] is True


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


def test_workspace_manager_uses_resolved_model_context(
    settings, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, int] = {}

    class CapturingProvider:
        def __init__(self, provider_settings) -> None:
            captured["context_limit"] = provider_settings.context_limit

    monkeypatch.setattr(runtime_module, "OpenAICompatibleProvider", CapturingProvider)
    storage.save_provider_config(
        ProviderConfig(
            model="deepseek-v4-pro",
            base_url="https://api.deepseek.com/v1",
            context_window=None,
        )
    )
    runtime = AgentRuntime(settings, storage, EventBroker(storage))

    manager = runtime.manager_for_workspace(settings.workspace)

    assert captured["context_limit"] == 1_000_000
    assert manager.settings.context_limit == 1_000_000
    assert runtime.model_context.source == "catalog"


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

from __future__ import annotations

import asyncio
import os
import secrets
import stat
from contextlib import suppress
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlparse

from traceforge.agent import AgentManager, RunConflictError
from traceforge.config import Settings
from traceforge.events import EventBroker
from traceforge.model_context import ResolvedModelContext, resolve_model_context
from traceforge.models import (
    ApprovalMode,
    InteractionMode,
    ProviderConfig,
    ReasoningEffort,
    RunRecord,
    RunState,
    utc_now,
)
from traceforge.provider import ModelProvider, OpenAICompatibleProvider, ProviderError
from traceforge.storage import Storage

_CREDENTIAL_MAX_BYTES = 16 * 1024
_MANAGED_CREDENTIAL_DIRECTORY = "provider-credentials"
_MANAGED_CREDENTIAL_PREFIX = "provider-credential-"
_LEGACY_MANAGED_CREDENTIAL_NAME = "provider-credential.key"
_PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "report_connection",
        "description": "Report that the model can use native tool calling.",
        "parameters": {
            "type": "object",
            "properties": {"status": {"type": "string", "enum": ["ok"]}},
            "required": ["status"],
            "additionalProperties": False,
        },
    },
}


def resolve_workspace(raw: str | Path) -> Path:
    candidate = Path(raw).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"Workspace is not a directory: {candidate}") from exc
    if not resolved.is_dir():
        raise ValueError(f"Workspace is not a directory: {resolved}")
    return resolved


def validate_credential_file(raw: str | Path) -> Path:
    candidate = Path(raw).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise ValueError(f"Credential file is not readable: {candidate}") from exc
    if not resolved.is_file():
        raise ValueError(f"Credential path is not a file: {resolved}")
    if metadata.st_size > _CREDENTIAL_MAX_BYTES:
        raise ValueError("Credential file must be smaller than 16 KiB")
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("Credential file must be owner-only; run chmod 600 on it")
    return resolved


def _normalized_api_key(raw: str) -> tuple[str, bytes]:
    value = raw.strip()
    encoded = value.encode("utf-8")
    if not value or "\n" in value or "\r" in value:
        raise ValueError("API key must contain exactly one non-empty line")
    if len(encoded) > _CREDENTIAL_MAX_BYTES:
        raise ValueError("API key must be smaller than 16 KiB")
    return value, encoded


def _open_managed_credential_directory(settings: Settings) -> tuple[Path, int]:
    data_dir = settings.data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    managed_directory = data_dir / _MANAGED_CREDENTIAL_DIRECTORY
    try:
        managed_directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    try:
        metadata = managed_directory.lstat()
    except OSError as exc:
        raise ValueError("Managed credential directory is not accessible") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError("Managed credential directory must not be a symbolic link")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("Managed credential path must be a directory")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_descriptor = os.open(managed_directory, flags)
    except OSError as exc:
        raise ValueError("Managed credential directory is not safe to open") from exc
    try:
        opened_metadata = os.fstat(directory_descriptor)
        if not stat.S_ISDIR(opened_metadata.st_mode):
            raise ValueError("Managed credential path must be a directory")
        if os.name == "posix":
            if opened_metadata.st_uid != os.getuid():
                raise ValueError(
                    "Managed credential directory must be owned by the current user"
                )
            os.fchmod(directory_descriptor, 0o700)
    except BaseException:
        os.close(directory_descriptor)
        raise
    return managed_directory, directory_descriptor


def store_managed_api_key(settings: Settings, raw: str) -> Path:
    """Store a provider key in a dedicated owner-only directory."""

    _, encoded = _normalized_api_key(raw)
    managed_directory, directory_descriptor = _open_managed_credential_directory(
        settings
    )
    descriptor = -1
    destination: Path | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        for _attempt in range(100):
            name = f"{_MANAGED_CREDENTIAL_PREFIX}{secrets.token_hex(16)}.key"
            try:
                descriptor = os.open(
                    name,
                    flags,
                    0o600,
                    dir_fd=directory_descriptor,
                )
            except FileExistsError:
                continue
            destination = managed_directory / name
            break
        if destination is None:
            raise OSError("Could not allocate a unique managed credential file")
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(encoded + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.fsync(directory_descriptor)
        return validate_credential_file(destination)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        if destination is not None:
            with suppress(OSError):
                os.unlink(destination.name, dir_fd=directory_descriptor)
        raise
    finally:
        os.close(directory_descriptor)


def _managed_credential_path(settings: Settings, raw: str | None) -> Path | None:
    if not raw:
        return None
    data_dir = settings.data_dir.resolve()
    candidate = Path(raw).expanduser()
    try:
        candidate_parent = candidate.parent.resolve(strict=False)
    except OSError:
        return None
    if (
        candidate_parent == data_dir
        and candidate.name == _LEGACY_MANAGED_CREDENTIAL_NAME
    ):
        return data_dir / _LEGACY_MANAGED_CREDENTIAL_NAME

    managed_directory = data_dir / _MANAGED_CREDENTIAL_DIRECTORY
    try:
        managed_metadata = managed_directory.lstat()
        managed_parent = managed_directory.resolve(strict=True)
    except OSError:
        return None
    if stat.S_ISLNK(managed_metadata.st_mode) or not stat.S_ISDIR(
        managed_metadata.st_mode
    ):
        return None
    if (
        candidate_parent == managed_parent
        and candidate.name.startswith(_MANAGED_CREDENTIAL_PREFIX)
        and candidate.name.endswith(".key")
    ):
        return managed_parent / candidate.name
    return None


def _read_api_key(settings: Settings, config: ProviderConfig) -> str:
    if config.credential_file:
        credential_file = validate_credential_file(config.credential_file)
        try:
            value = credential_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise ValueError("Credential file could not be read as UTF-8") from exc
        if not value or "\n" in value or "\r" in value:
            raise ValueError("Credential file must contain exactly one non-empty line")
        return value
    if settings.api_key:
        return settings.api_key
    raise ValueError("Configure a credential file or set OPENAI_API_KEY before starting a run")


class AgentRuntime:
    """Routes persisted runs to one workspace-bound manager per canonical directory."""

    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        broker: EventBroker,
        *,
        provider_override: ModelProvider | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.broker = broker
        self.provider_override = provider_override
        self._managers: dict[Path, AgentManager] = {}
        self._provider_lock = asyncio.Lock()
        self._resumed_runs: set[str] = set()
        self._resume_watchers: set[asyncio.Task[None]] = set()

    @property
    def provider_config(self) -> ProviderConfig:
        return self.storage.get_provider_config(
            ProviderConfig(model=self.settings.model, base_url=self.settings.base_url)
        )

    @property
    def model_context(self) -> ResolvedModelContext:
        config = self.provider_config
        return resolve_model_context(
            config.model,
            base_url=config.base_url,
            configured_window=config.context_window,
            fallback_window=self.settings.context_limit,
        )

    def credential_configured(self, config: ProviderConfig | None = None) -> bool:
        selected = config or self.provider_config
        if selected.credential_file:
            try:
                _read_api_key(self.settings, selected)
            except ValueError:
                return False
            return True
        return bool(self.settings.api_key) or self.provider_override is not None

    def connection_verified(self) -> bool:
        if self.provider_override is not None:
            return True
        return (
            self.credential_configured()
            and self.storage.get_provider_verified_at() is not None
        )

    def require_connection_verified(self) -> None:
        if not self.credential_configured():
            raise ValueError(
                "Configure a credential file or set OPENAI_API_KEY before starting a run"
            )
        if not self.connection_verified():
            raise ValueError(
                "Test and verify the model connection before starting or continuing a task"
            )

    async def start_run(
        self,
        task: str,
        workspace: str | Path,
        *,
        verifier_enabled: bool = True,
        project_id: str | None = None,
        mode: InteractionMode = InteractionMode.AGENT,
        approval_mode: ApprovalMode = ApprovalMode.AUTOMATIC,
        reasoning_effort: ReasoningEffort = ReasoningEffort.AUTO,
    ) -> RunRecord:
        async with self._provider_lock:
            self.require_connection_verified()
            manager = self.manager_for_workspace(workspace)
            return await manager.start_run(
                task,
                verifier_enabled=verifier_enabled,
                project_id=project_id,
                mode=mode,
                approval_mode=approval_mode,
                reasoning_effort=reasoning_effort,
            )

    async def follow_up(
        self,
        run_id: str,
        prompt: str,
        *,
        mode: InteractionMode = InteractionMode.AGENT,
        approval_mode: ApprovalMode = ApprovalMode.AUTOMATIC,
        reasoning_effort: ReasoningEffort = ReasoningEffort.AUTO,
    ) -> RunRecord:
        async with self._provider_lock:
            self.require_connection_verified()
            manager = self.manager_for_run(run_id)
            if self.storage.get_run(run_id).state is RunState.ROLLED_BACK:
                return await manager.continue_after_rollback(
                    run_id,
                    prompt,
                    mode=mode,
                    approval_mode=approval_mode,
                    reasoning_effort=reasoning_effort,
                )
            return await manager.follow_up(
                run_id,
                prompt,
                mode=mode,
                approval_mode=approval_mode,
                reasoning_effort=reasoning_effort,
            )

    async def resume_run(self, run_id: str) -> RunRecord:
        async with self._provider_lock:
            self.require_connection_verified()
            manager = self.manager_for_run(run_id)
            run = await manager.resume(run_id)
            self._track_resumed_run(manager, run_id)
            return run

    def _track_resumed_run(self, manager: AgentManager, run_id: str) -> None:
        self._resumed_runs.add(run_id)

        async def wait_for_completion() -> None:
            try:
                await manager.wait(run_id)
            finally:
                self._resumed_runs.discard(run_id)

        watcher = asyncio.create_task(
            wait_for_completion(), name=f"traceforge:resume-watch:{run_id}"
        )
        self._resume_watchers.add(watcher)
        watcher.add_done_callback(self._resume_watchers.discard)

    def _require_provider_change_idle(self) -> None:
        if self.storage.has_live_run() or self._resumed_runs:
            raise RunConflictError(
                "Pause, stop, or finish running work before changing model settings"
            )

    def manager_for_run(self, run_id: str) -> AgentManager:
        return self.manager_for_workspace(self.storage.get_run(run_id).workspace)

    def existing_manager_for_run(self, run_id: str) -> AgentManager | None:
        """Return an already-loaded manager without constructing a provider."""

        run = self.storage.get_run(run_id)
        return self._managers.get(Path(run.workspace))

    def manager_for_workspace(self, workspace: str | Path) -> AgentManager:
        resolved = resolve_workspace(workspace)
        manager = self._managers.get(resolved)
        if manager is not None:
            return manager
        config = self.provider_config
        model_context = resolve_model_context(
            config.model,
            base_url=config.base_url,
            configured_window=config.context_window,
            fallback_window=self.settings.context_limit,
        )
        if self.provider_override is None:
            api_key = _read_api_key(self.settings, config)
            run_settings = replace(
                self.settings,
                workspace=resolved,
                api_key=api_key,
                model=config.model,
                base_url=config.base_url,
                credential_file=(Path(config.credential_file) if config.credential_file else None),
                context_limit=model_context.context_window,
            )
            provider: ModelProvider = OpenAICompatibleProvider(run_settings)
        else:
            run_settings = replace(
                self.settings,
                workspace=resolved,
                model=config.model,
                base_url=config.base_url,
                credential_file=(Path(config.credential_file) if config.credential_file else None),
                context_limit=model_context.context_window,
            )
            provider = self.provider_override
        manager = AgentManager(run_settings, self.storage, provider, broker=self.broker)
        self._managers[resolved] = manager
        return manager

    def _normalized_provider_config(self, config: ProviderConfig) -> ProviderConfig:
        normalized = config.model_copy(deep=True)
        if normalized.credential_file is not None:
            normalized.credential_file = normalized.credential_file.strip() or None
        if normalized.credential_file:
            normalized.credential_file = str(
                validate_credential_file(normalized.credential_file)
            )
        normalized.model = normalized.model.strip()
        if not normalized.model:
            raise ValueError("Model must not be empty")
        if normalized.base_url is not None:
            normalized.base_url = normalized.base_url.strip() or None
        if normalized.base_url is not None:
            try:
                parsed = urlparse(normalized.base_url)
                hostname, _port = parsed.hostname, parsed.port
            except ValueError as exc:
                raise ValueError(
                    "Base URL must be an absolute http:// or https:// URL"
                ) from exc
            if parsed.scheme not in {"http", "https"} or not hostname:
                raise ValueError(
                    "Base URL must be an absolute http:// or https:// URL"
                )
        return normalized

    def _probe_api_key(self, config: ProviderConfig, api_key: str | None) -> str:
        if api_key is not None:
            if config.credential_file:
                raise ValueError("Choose either an API key or a credential file, not both")
            return _normalized_api_key(api_key)[0]
        if self.provider_override is not None:
            return ""
        return _read_api_key(self.settings, config)

    @staticmethod
    def _same_provider_config(left: ProviderConfig, right: ProviderConfig) -> bool:
        return (
            left.model,
            left.base_url,
            left.credential_file,
            left.context_window,
        ) == (
            right.model,
            right.base_url,
            right.credential_file,
            right.context_window,
        )

    async def _persist_provider_config(
        self,
        config: ProviderConfig,
        *,
        api_key: str | None,
        verified_at: datetime | None,
    ) -> ProviderConfig:
        previous = self.provider_config
        managed_credential: Path | None = None
        if api_key is not None:
            if config.credential_file:
                raise ValueError("Choose either an API key or a credential file, not both")
            managed_credential = store_managed_api_key(self.settings, api_key)
            config.credential_file = str(managed_credential)
        try:
            await self.shutdown()
            self.storage.save_provider_config(config, verified_at=verified_at)
        except BaseException:
            if managed_credential is not None:
                await asyncio.to_thread(managed_credential.unlink, missing_ok=True)
            raise
        previous_managed_credential = _managed_credential_path(
            self.settings, previous.credential_file
        )
        if (
            previous.credential_file != config.credential_file
            and previous_managed_credential is not None
        ):
            with suppress(OSError):
                await asyncio.to_thread(
                    previous_managed_credential.unlink,
                    missing_ok=True,
                )
        return self.provider_config

    async def save_provider_config(
        self, config: ProviderConfig, *, api_key: str | None = None
    ) -> ProviderConfig:
        async with self._provider_lock:
            self._require_provider_change_idle()
            normalized = self._normalized_provider_config(config)
            if api_key is not None:
                self._probe_api_key(normalized, api_key)
            return await self._persist_provider_config(
                normalized,
                api_key=api_key,
                verified_at=None,
            )

    async def test_and_save_provider_config(
        self, config: ProviderConfig, *, api_key: str | None = None
    ) -> dict[str, Any]:
        async with self._provider_lock:
            self._require_provider_change_idle()
            normalized = self._normalized_provider_config(config)
            saved = self.provider_config
            testing_saved_config = api_key is None and self._same_provider_config(
                normalized, saved
            )
            selected_api_key = self._probe_api_key(normalized, api_key)
            result = await self._probe_connection(normalized, selected_api_key)
            if not result["ok"]:
                if (
                    testing_saved_config
                    and self.storage.get_provider_verified_at() is not None
                ):
                    self.storage.save_provider_config(saved, verified_at=None)
                return result
            await self._persist_provider_config(
                normalized,
                api_key=api_key,
                verified_at=utc_now(),
            )
            return result

    async def test_connection(self) -> dict[str, Any]:
        async with self._provider_lock:
            config = self.provider_config
            if self.provider_override is not None:
                return {
                    "ok": True,
                    "model": config.model,
                    "latency_ms": 0,
                    "detail": "Scripted provider is ready.",
                }
            self._require_provider_change_idle()
            api_key = self._probe_api_key(config, None)
            result = await self._probe_connection(config, api_key)
            if result["ok"]:
                await self._persist_provider_config(
                    config,
                    api_key=None,
                    verified_at=utc_now(),
                )
            elif self.storage.get_provider_verified_at() is not None:
                self.storage.save_provider_config(config, verified_at=None)
            return result

    async def _probe_connection(
        self, config: ProviderConfig, api_key: str
    ) -> dict[str, Any]:
        if self.provider_override is not None:
            return {
                "ok": True,
                "model": config.model,
                "latency_ms": 0,
                "detail": "Scripted provider is ready.",
            }
        probe_settings = replace(
            self.settings,
            api_key=api_key,
            model=config.model,
            base_url=config.base_url,
        )
        provider = OpenAICompatibleProvider(probe_settings)
        started = monotonic()
        try:
            response = await provider.complete(
                [
                    {
                        "role": "system",
                        "content": (
                            "This is a connection probe. Call report_connection exactly once "
                            "with status=ok and do not answer in prose."
                        ),
                    },
                    {"role": "user", "content": "Verify native tool calling now."},
                ],
                [_PROBE_TOOL],
            )
        except ProviderError as exc:
            detail = str(exc)
            if api_key:
                detail = detail.replace(api_key, "[REDACTED]")
            return {
                "ok": False,
                "model": config.model,
                "latency_ms": round((monotonic() - started) * 1_000),
                "detail": detail,
            }
        valid = any(
            call.name == "report_connection" and call.arguments.get("status") == "ok"
            for call in response.tool_calls
        )
        return {
            "ok": valid,
            "model": config.model,
            "latency_ms": round((monotonic() - started) * 1_000),
            "detail": (
                "Connection and native tool calling verified."
                if valid
                else "The model responded, but did not complete the native tool-call probe."
            ),
        }

    async def shutdown(self) -> None:
        managers = list(self._managers.values())
        self._managers.clear()
        if managers:
            await asyncio.gather(*(manager.shutdown() for manager in managers))
        watchers = list(self._resume_watchers)
        if watchers:
            await asyncio.gather(*watchers, return_exceptions=True)

from __future__ import annotations

import asyncio
import os
import stat
from dataclasses import replace
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlparse

from traceforge.agent import AgentManager, RunConflictError
from traceforge.config import Settings
from traceforge.events import EventBroker
from traceforge.models import ProviderConfig, RunRecord
from traceforge.provider import ModelProvider, OpenAICompatibleProvider, ProviderError
from traceforge.storage import Storage

_CREDENTIAL_MAX_BYTES = 16 * 1024
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

    @property
    def provider_config(self) -> ProviderConfig:
        return self.storage.get_provider_config(
            ProviderConfig(model=self.settings.model, base_url=self.settings.base_url)
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

    async def start_run(
        self,
        task: str,
        workspace: str | Path,
        *,
        verifier_enabled: bool = True,
        project_id: str | None = None,
    ) -> RunRecord:
        manager = self.manager_for_workspace(workspace)
        return await manager.start_run(
            task,
            verifier_enabled=verifier_enabled,
            project_id=project_id,
        )

    def manager_for_run(self, run_id: str) -> AgentManager:
        return self.manager_for_workspace(self.storage.get_run(run_id).workspace)

    def manager_for_workspace(self, workspace: str | Path) -> AgentManager:
        resolved = resolve_workspace(workspace)
        manager = self._managers.get(resolved)
        if manager is not None:
            return manager
        config = self.provider_config
        if self.provider_override is None:
            api_key = _read_api_key(self.settings, config)
            run_settings = replace(
                self.settings,
                workspace=resolved,
                api_key=api_key,
                model=config.model,
                base_url=config.base_url,
                credential_file=(
                    Path(config.credential_file) if config.credential_file else None
                ),
            )
            provider: ModelProvider = OpenAICompatibleProvider(run_settings)
        else:
            run_settings = replace(
                self.settings,
                workspace=resolved,
                model=config.model,
                base_url=config.base_url,
                credential_file=(
                    Path(config.credential_file) if config.credential_file else None
                ),
            )
            provider = self.provider_override
        manager = AgentManager(run_settings, self.storage, provider, broker=self.broker)
        self._managers[resolved] = manager
        return manager

    async def save_provider_config(self, config: ProviderConfig) -> ProviderConfig:
        if self.storage.has_live_run():
            raise RunConflictError(
                "Pause, stop, or finish running work before changing model settings"
            )
        if config.credential_file is not None:
            config.credential_file = config.credential_file.strip() or None
        if config.credential_file:
            config.credential_file = str(validate_credential_file(config.credential_file))
        config.model = config.model.strip()
        if not config.model:
            raise ValueError("Model must not be empty")
        if config.base_url is not None:
            config.base_url = config.base_url.strip() or None
        if config.base_url is not None:
            parsed = urlparse(config.base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("Base URL must be an absolute http:// or https:// URL")
        await self.shutdown()
        self.storage.save_provider_config(config)
        return self.provider_config

    async def test_connection(self) -> dict[str, Any]:
        config = self.provider_config
        if self.provider_override is not None:
            return {
                "ok": True,
                "model": config.model,
                "latency_ms": 0,
                "detail": "Scripted provider is ready.",
            }
        api_key = _read_api_key(self.settings, config)
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
            return {
                "ok": False,
                "model": config.model,
                "latency_ms": round((monotonic() - started) * 1_000),
                "detail": str(exc).replace(api_key, "[REDACTED]"),
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

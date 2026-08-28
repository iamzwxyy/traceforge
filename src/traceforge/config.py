from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path, user_documents_path


@dataclass(frozen=True, slots=True)
class Settings:
    workspace: Path
    data_dir: Path
    api_key: str
    base_url: str | None
    model: str
    credential_file: Path | None = None
    context_limit: int = 64_000
    model_request_timeout: int = 180
    model_retry_attempts: int = 3
    model_retry_delay: float = 1.0
    command_timeout: int = 120
    max_command_timeout: int = 600
    max_steps: int = 30
    max_repair_cycles: int = 2
    stored_output_limit: int = 1024 * 1024
    model_output_limit: int = 16 * 1024
    suggested_task: str | None = None
    demo_mode: bool = False

    @classmethod
    def from_env(
        cls, workspace: Path | None = None, *, require_api_key: bool = True
    ) -> Settings:
        selected_workspace = workspace or _default_workspace_path()
        if workspace is None:
            try:
                # Existing roots may be intentionally shared; direct-task children are always 0700.
                selected_workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError as exc:
                raise ValueError(
                    f"Default workspace root could not be created: {selected_workspace}"
                ) from exc
        try:
            resolved = selected_workspace.expanduser().resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"Workspace is not a directory: {selected_workspace}") from exc
        if not resolved.is_dir():
            raise ValueError(f"Workspace is not a directory: {resolved}")
        api_key = os.getenv("OPENAI_API_KEY", "")
        if require_api_key and not api_key:
            raise ValueError("OPENAI_API_KEY is required")
        context_limit = _positive_int("TRACEFORGE_CONTEXT_LIMIT", 64_000)
        model_request_timeout = _positive_int("TRACEFORGE_MODEL_TIMEOUT", 180)
        return cls(
            workspace=resolved,
            data_dir=user_data_path("TraceForge", "TraceForge", ensure_exists=True),
            api_key=api_key,
            base_url=os.getenv("OPENAI_BASE_URL") or None,
            model=os.getenv("OPENAI_MODEL", "gpt-5.6-sol"),
            context_limit=context_limit,
            model_request_timeout=model_request_timeout,
        )

    @property
    def masked_base_url(self) -> str:
        return self.base_url or "https://api.openai.com/v1"


def _default_workspace_path() -> Path:
    configured = os.getenv("TRACEFORGE_WORKSPACE_ROOT", "").strip()
    return (
        Path(configured).expanduser()
        if configured
        else user_documents_path() / "TraceForge"
    )


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value

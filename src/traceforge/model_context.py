from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from traceforge.model_reasoning import is_official_deepseek_endpoint

ContextWindowSource = Literal["configured", "catalog", "fallback"]


@dataclass(frozen=True, slots=True)
class ResolvedModelContext:
    context_window: int
    source: ContextWindowSource


# Keep this catalog deliberately small and exact. Provider model ids are dynamic, so an
# unfamiliar name must remain usable with a conservative fallback rather than being rejected or
# assigned a guessed family capacity. These entries mirror the pinned DeepSeek Harness adapter
# catalog used by TraceForge's SOTA benchmark.
_KNOWN_CONTEXT_WINDOWS = {
    "deepseek-v4-flash": 1_000_000,
    "deepseek-v4-pro": 1_000_000,
    "deepseek-v4-flash-vision-exp": 1_000_000,
}

_MAX_CONTEXT_WINDOW = 10_000_000


def resolve_model_context(
    model: str,
    *,
    base_url: str | None,
    configured_window: int | None,
    fallback_window: int,
) -> ResolvedModelContext:
    """Resolve one model's capacity without treating the catalog as a routing whitelist."""

    if configured_window is not None and not 1 <= configured_window <= _MAX_CONTEXT_WINDOW:
        raise ValueError("configured_window must be between 1 and 10000000")
    if not 1 <= fallback_window <= _MAX_CONTEXT_WINDOW:
        raise ValueError("fallback_window must be between 1 and 10000000")

    if configured_window is not None:
        return ResolvedModelContext(configured_window, "configured")

    normalized = model.strip().casefold()
    if is_official_deepseek_endpoint(base_url):
        if context_window := _KNOWN_CONTEXT_WINDOWS.get(normalized):
            return ResolvedModelContext(context_window, "catalog")
    return ResolvedModelContext(fallback_window, "fallback")

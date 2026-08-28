from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from traceforge.models import ReasoningEffort

ReasoningCapabilitySource = Literal[
    "openai_catalog", "deepseek_catalog", "provider_default"
]
ReasoningTransport = Literal["openai_chat", "deepseek_chat", "omit"]

CATALOG_VERSION = "2026-08-28"
_OFFICIAL_ENDPOINT_PATTERN = re.compile(
    r"^([a-z][a-z0-9+.-]*)://([^/?#]*)([^?#]*)(?:\?([^#]*))?(?:#(.*))?$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ReasoningCapability:
    """Exact route/model reasoning contract advertised to both API and provider."""

    supported_efforts: tuple[ReasoningEffort, ...]
    default_effort: ReasoningEffort | None
    source: ReasoningCapabilitySource
    transport: ReasoningTransport

    @property
    def adjustable(self) -> bool:
        return len(self.supported_efforts) > 1

    def validate(self, effort: ReasoningEffort) -> None:
        if effort in self.supported_efforts:
            return
        supported = ", ".join(item.value for item in self.supported_efforts)
        raise ValueError(
            f"Reasoning effort '{effort.value}' is not supported by this exact model route; "
            f"choose one of: {supported}"
        )


_AUTO = ReasoningEffort.AUTO

_GPT_56_EFFORTS = (
    _AUTO,
    ReasoningEffort.NONE,
    ReasoningEffort.LOW,
    ReasoningEffort.MEDIUM,
    ReasoningEffort.HIGH,
    ReasoningEffort.XHIGH,
    ReasoningEffort.MAX,
)
_GPT_54_EFFORTS = (
    _AUTO,
    ReasoningEffort.NONE,
    ReasoningEffort.LOW,
    ReasoningEffort.MEDIUM,
    ReasoningEffort.HIGH,
    ReasoningEffort.XHIGH,
)

# The catalog is intentionally exact and small. A family-looking custom model name must not gain
# protocol fields merely because it contains a familiar substring.
_OPENAI_MODELS: dict[str, tuple[tuple[ReasoningEffort, ...], ReasoningEffort]] = {
    "gpt-5.6": (_GPT_56_EFFORTS, ReasoningEffort.MEDIUM),
    "gpt-5.6-sol": (_GPT_56_EFFORTS, ReasoningEffort.MEDIUM),
    "gpt-5.6-terra": (_GPT_56_EFFORTS, ReasoningEffort.MEDIUM),
    "gpt-5.6-luna": (_GPT_56_EFFORTS, ReasoningEffort.MEDIUM),
    "gpt-5.5": (
        (
            _AUTO,
            ReasoningEffort.NONE,
            ReasoningEffort.LOW,
            ReasoningEffort.MEDIUM,
            ReasoningEffort.HIGH,
            ReasoningEffort.XHIGH,
        ),
        ReasoningEffort.MEDIUM,
    ),
    "gpt-5.4": (_GPT_54_EFFORTS, ReasoningEffort.NONE),
    "gpt-5.4-mini": (_GPT_54_EFFORTS, ReasoningEffort.NONE),
    "gpt-5.4-nano": (_GPT_54_EFFORTS, ReasoningEffort.NONE),
    "gpt-5.3-codex": (
        (
            _AUTO,
            ReasoningEffort.LOW,
            ReasoningEffort.MEDIUM,
            ReasoningEffort.HIGH,
            ReasoningEffort.XHIGH,
        ),
        ReasoningEffort.MEDIUM,
    ),
    "gpt-5": (
        (
            _AUTO,
            ReasoningEffort.MINIMAL,
            ReasoningEffort.LOW,
            ReasoningEffort.MEDIUM,
            ReasoningEffort.HIGH,
        ),
        ReasoningEffort.MEDIUM,
    ),
}

_DEEPSEEK_MODELS = {
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "deepseek-v4-flash-vision-exp",
}
_DEEPSEEK_EFFORTS = (
    _AUTO,
    ReasoningEffort.NONE,
    ReasoningEffort.LOW,
    ReasoningEffort.HIGH,
    ReasoningEffort.MAX,
)


def resolve_reasoning_capability(
    model: str, *, base_url: str | None
) -> ReasoningCapability:
    normalized = model.strip().casefold()
    if is_official_openai_endpoint(base_url) and normalized in _OPENAI_MODELS:
        efforts, default = _OPENAI_MODELS[normalized]
        return ReasoningCapability(
            supported_efforts=efforts,
            default_effort=default,
            source="openai_catalog",
            transport="openai_chat",
        )
    if is_official_deepseek_endpoint(base_url) and normalized in _DEEPSEEK_MODELS:
        return ReasoningCapability(
            supported_efforts=_DEEPSEEK_EFFORTS,
            default_effort=ReasoningEffort.HIGH,
            source="deepseek_catalog",
            transport="deepseek_chat",
        )
    return ReasoningCapability(
        supported_efforts=(_AUTO,),
        default_effort=None,
        source="provider_default",
        transport="omit",
    )


def is_official_openai_endpoint(base_url: str | None) -> bool:
    if base_url is None or not base_url.strip():
        return True
    return _matches_endpoint(base_url, hostname="api.openai.com")


def is_official_deepseek_endpoint(base_url: str | None) -> bool:
    if not base_url:
        return False
    return _matches_endpoint(base_url, hostname="api.deepseek.com")


def _matches_endpoint(raw: str, *, hostname: str) -> bool:
    parsed = _OFFICIAL_ENDPOINT_PATTERN.fullmatch(raw.strip())
    if parsed is None:
        return False
    scheme, authority, path, query, fragment = parsed.groups()
    return (
        scheme.casefold() == "https"
        and authority.casefold() == hostname
        and path.rstrip("/") in {"", "/v1"}
        and not query
        and not fragment
    )

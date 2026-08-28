from __future__ import annotations

import pytest

from traceforge.model_context import resolve_model_context


def test_context_override_wins_over_exact_catalog() -> None:
    resolved = resolve_model_context(
        "deepseek-v4-flash",
        base_url="https://gateway.example/v1",
        configured_window=240_000,
        fallback_window=64_000,
    )

    assert resolved.context_window == 240_000
    assert resolved.source == "configured"


@pytest.mark.parametrize(
    "model",
    [
        "deepseek-v4-flash",
        "DeepSeek-V4-Pro",
        "deepseek-v4-flash-vision-exp",
    ],
)
def test_exact_deepseek_v4_catalog_models_resolve_to_one_million(model: str) -> None:
    resolved = resolve_model_context(
        model,
        base_url="https://api.deepseek.com/v1",
        configured_window=None,
        fallback_window=64_000,
    )

    assert resolved.context_window == 1_000_000
    assert resolved.source == "catalog"


@pytest.mark.parametrize(
    "model",
    [
        "deepseek-chat",
        "deepseek-v4-flash-preview",
        "not-deepseek-v4-pro-really",
        "gateway/deepseek-v4-pro",
        "gateway/deepseek-v4-pro/preview",
    ],
)
def test_unknown_or_deceptive_model_names_use_conservative_fallback(model: str) -> None:
    resolved = resolve_model_context(
        model,
        base_url="https://api.deepseek.com",
        configured_window=None,
        fallback_window=72_000,
    )

    assert resolved.context_window == 72_000
    assert resolved.source == "fallback"


@pytest.mark.parametrize(
    ("configured", "fallback"),
    [(0, 64_000), (10_000_001, 64_000), (None, 0), (None, 10_000_001)],
)
def test_context_resolver_rejects_unsafe_capacities(
    configured: int | None,
    fallback: int,
) -> None:
    with pytest.raises(ValueError, match="must be between"):
        resolve_model_context(
            "unknown",
            base_url=None,
            configured_window=configured,
            fallback_window=fallback,
        )


@pytest.mark.parametrize(
    "base_url",
    [
        None,
        "http://api.deepseek.com",
        "https://api.deepseek.com.evil.example",
        "https://api.deepseek.com:8443/v1",
        "https://gateway.example/v1",
        "https://api.deepseek.com/v1/models",
    ],
)
def test_official_model_id_on_other_endpoints_does_not_inherit_catalog(
    base_url: str | None,
) -> None:
    resolved = resolve_model_context(
        "deepseek-v4-pro",
        base_url=base_url,
        configured_window=None,
        fallback_window=64_000,
    )

    assert resolved.context_window == 64_000
    assert resolved.source == "fallback"

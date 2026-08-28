from __future__ import annotations

import pytest

from traceforge.model_reasoning import (
    CATALOG_VERSION,
    is_official_deepseek_endpoint,
    is_official_openai_endpoint,
    resolve_reasoning_capability,
)
from traceforge.models import ReasoningEffort


@pytest.mark.parametrize("base_url", [None, "", "https://api.openai.com", "https://api.openai.com/v1/"])
def test_openai_exact_route_advertises_model_specific_order(base_url: str | None) -> None:
    capability = resolve_reasoning_capability("gpt-5.6-sol", base_url=base_url)

    assert capability.supported_efforts == (
        ReasoningEffort.AUTO,
        ReasoningEffort.NONE,
        ReasoningEffort.LOW,
        ReasoningEffort.MEDIUM,
        ReasoningEffort.HIGH,
        ReasoningEffort.XHIGH,
        ReasoningEffort.MAX,
    )
    assert capability.default_effort is ReasoningEffort.MEDIUM
    assert capability.source == "openai_catalog"
    assert capability.transport == "openai_chat"
    assert CATALOG_VERSION == "2026-08-28"


def test_openai_catalog_does_not_invent_shared_efforts() -> None:
    gpt_54 = resolve_reasoning_capability("gpt-5.4", base_url=None)
    codex = resolve_reasoning_capability("gpt-5.3-codex", base_url=None)
    original = resolve_reasoning_capability("gpt-5", base_url=None)

    assert gpt_54.default_effort is ReasoningEffort.NONE
    assert ReasoningEffort.MAX not in gpt_54.supported_efforts
    assert ReasoningEffort.NONE not in codex.supported_efforts
    assert ReasoningEffort.MINIMAL in original.supported_efforts
    assert ReasoningEffort.NONE not in original.supported_efforts


@pytest.mark.parametrize("model", ["gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"])
def test_openai_56_alias_and_variants_share_the_documented_contract(model: str) -> None:
    capability = resolve_reasoning_capability(model, base_url=None)

    assert capability.default_effort is ReasoningEffort.MEDIUM
    assert capability.supported_efforts[-1] is ReasoningEffort.MAX


@pytest.mark.parametrize("model", ["gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano"])
def test_openai_54_models_share_the_documented_contract(model: str) -> None:
    capability = resolve_reasoning_capability(model, base_url=None)

    assert capability.default_effort is ReasoningEffort.NONE
    assert ReasoningEffort.XHIGH in capability.supported_efforts
    assert ReasoningEffort.MAX not in capability.supported_efforts


@pytest.mark.parametrize(
    "model",
    ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-flash-vision-exp"],
)
def test_deepseek_exact_models_advertise_only_distinct_behaviors(model: str) -> None:
    capability = resolve_reasoning_capability(
        model, base_url="https://api.deepseek.com/v1"
    )

    assert capability.supported_efforts == (
        ReasoningEffort.AUTO,
        ReasoningEffort.NONE,
        ReasoningEffort.LOW,
        ReasoningEffort.HIGH,
        ReasoningEffort.MAX,
    )
    assert capability.default_effort is ReasoningEffort.HIGH
    assert capability.source == "deepseek_catalog"
    assert capability.transport == "deepseek_chat"


@pytest.mark.parametrize(
    ("base_url", "model"),
    [
        ("http://api.openai.com/v1", "gpt-5.6-sol"),
        ("https://api.openai.com.evil.example/v1", "gpt-5.6-sol"),
        ("https://api.openai.com:8443/v1", "gpt-5.6-sol"),
        ("https://api.openai.com/v1?route=other", "gpt-5.6-sol"),
        ("https://api.openai.com/v1", "gateway/gpt-5.6-sol"),
        ("https://api.openai.com/v1", "gpt-5.6-sol-preview"),
        ("https://api.deepseek.com/v1/models", "deepseek-v4-pro"),
        ("https://user@api.deepseek.com/v1", "deepseek-v4-pro"),
        ("https://api.deepseek.com/v1", "not-deepseek-v4-pro-really"),
        ("https://provider.example/v1", "gpt-5.6-sol"),
    ],
)
def test_unknown_or_deceptive_routes_remain_auto_only(
    base_url: str, model: str
) -> None:
    capability = resolve_reasoning_capability(model, base_url=base_url)

    assert capability.supported_efforts == (ReasoningEffort.AUTO,)
    assert capability.default_effort is None
    assert capability.source == "provider_default"
    assert capability.transport == "omit"
    with pytest.raises(ValueError, match="not supported"):
        capability.validate(ReasoningEffort.HIGH)


def test_endpoint_helpers_are_strict() -> None:
    assert is_official_openai_endpoint(None)
    assert is_official_openai_endpoint("https://api.openai.com/v1")
    assert not is_official_openai_endpoint("https://api.openai.com:/v1")
    assert not is_official_openai_endpoint("https://api.openai.com/v1/.")
    assert not is_official_openai_endpoint("http://api.openai.com/v1")
    assert is_official_deepseek_endpoint("https://api.deepseek.com")
    assert not is_official_deepseek_endpoint("https://api.deepseek.com:443/v1")


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.openai.com:bad/v1",
        "https://api.openai.com:99999/v1",
        "https://[api.openai.com/v1",
    ],
)
def test_malformed_official_looking_endpoints_fail_closed(base_url: str) -> None:
    assert not is_official_openai_endpoint(base_url)
    capability = resolve_reasoning_capability("gpt-5.6-sol", base_url=base_url)
    assert capability.supported_efforts == (ReasoningEffort.AUTO,)

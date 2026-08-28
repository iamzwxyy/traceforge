from __future__ import annotations

import pytest

from traceforge.context import ContextManager


def test_context_compaction_preserves_ends_and_tool_evidence() -> None:
    manager = ContextManager(200, threshold=0.5)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "original task"},
        *[
            {"role": "tool", "name": "read_file", "content": f"evidence {index}"}
            for index in range(20)
        ],
    ]

    compacted, changed = manager.prepare(messages)

    assert changed
    assert compacted[0] == messages[0]
    assert compacted[1] == messages[1]
    assert "compacted deterministically" in compacted[2]["content"]
    assert compacted[-1] == messages[-1]


def test_context_compaction_keeps_tool_request_and_result_adjacent() -> None:
    manager = ContextManager(240, threshold=0.5, retain_ratio=0.1)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "original task"},
        *[
            {"role": "user", "content": f"older evidence {index} " * 12}
            for index in range(8)
        ],
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "read", "function": {"name": "read_file"}}],
        },
        {"role": "tool", "tool_call_id": "read", "content": "current evidence"},
    ]

    compacted, changed = manager.prepare(messages)

    assert changed
    assert compacted[-2:] == messages[-2:]


def test_context_compaction_accounts_for_tool_schema_surface() -> None:
    manager = ContextManager(320, threshold=0.5, retain_ratio=0.1)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
        *[
            {"role": "tool", "content": f"historical result {index} " * 8}
            for index in range(12)
        ],
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "large_tool",
                "description": "schema surface " * 80,
                "parameters": {"type": "object"},
            },
        }
    ]

    compacted, changed = manager.prepare(messages, tools)

    assert changed
    assert compacted[-1] == messages[-1]


@pytest.mark.parametrize(
    ("threshold", "retain_ratio"),
    [(1.0, 0.1), (0.8, 0.8), (0.8, 0.0)],
)
def test_context_manager_rejects_invalid_compaction_ratios(
    threshold: float,
    retain_ratio: float,
) -> None:
    with pytest.raises(ValueError):
        ContextManager(1_000, threshold=threshold, retain_ratio=retain_ratio)

from __future__ import annotations

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


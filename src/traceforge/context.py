from __future__ import annotations

import json
from typing import Any


class ContextManager:
    def __init__(self, context_limit: int, *, threshold: float = 0.7) -> None:
        self.context_limit = context_limit
        self.threshold = threshold

    def estimated_tokens(self, messages: list[dict[str, Any]]) -> int:
        serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
        return max(1, len(serialized.encode("utf-8")) // 4)

    def prepare(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        if self.estimated_tokens(messages) < self.context_limit * self.threshold:
            return messages.copy(), False
        if len(messages) <= 14:
            return messages.copy(), False
        protected = messages[:2]
        recent = messages[-12:]
        middle = messages[2:-12]
        summary = self._summarize(middle)
        compacted = [
            *protected,
            {
                "role": "system",
                "content": (
                    "Earlier execution history was compacted deterministically. "
                    "This summary is evidence-oriented, not hidden reasoning:\n" + summary
                ),
            },
            *recent,
        ]
        return compacted, True

    @staticmethod
    def _summarize(messages: list[dict[str, Any]]) -> str:
        rows: list[str] = []
        for message in messages:
            role = str(message.get("role", "unknown"))
            if role == "tool":
                name = message.get("name", "tool")
                content = str(message.get("content", ""))
                rows.append(f"- tool {name}: {content[:400]}")
                continue
            if message.get("tool_calls"):
                names = [
                    call.get("function", {}).get("name", "unknown")
                    for call in message["tool_calls"]
                ]
                rows.append(f"- assistant requested tools: {', '.join(names)}")
            raw_content = message.get("content")
            if raw_content:
                rows.append(f"- {role}: {str(raw_content)[:400]}")
        return "\n".join(rows) or "- No material messages were removed."

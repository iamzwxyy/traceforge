from __future__ import annotations

import json
from typing import Any


class ContextManager:
    def __init__(
        self,
        context_limit: int,
        *,
        threshold: float = 0.8,
        retain_ratio: float = 0.16,
    ) -> None:
        if context_limit <= 0:
            raise ValueError("context_limit must be positive")
        if not 0 < retain_ratio < threshold < 1:
            raise ValueError(
                "retain_ratio must be below threshold and both must be between 0 and 1"
            )
        self.context_limit = context_limit
        self.threshold = threshold
        self.retain_ratio = retain_ratio

    def estimated_tokens(self, messages: list[dict[str, Any]]) -> int:
        serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
        return max(1, len(serialized.encode("utf-8")) // 4)

    def prepare(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        request_surface = {"messages": messages, "tools": tools or []}
        if self.estimated_tokens([request_surface]) < self.context_limit * self.threshold:
            return messages.copy(), False
        if len(messages) <= 2:
            return messages.copy(), False
        protected = messages[:2]
        units = self._message_units(messages[2:])
        recent_units = self._recent_units(
            units, token_budget=max(1, int(self.context_limit * self.retain_ratio))
        )
        removed_count = len(units) - len(recent_units)
        if removed_count <= 0:
            return messages.copy(), False
        middle = [message for unit in units[:removed_count] for message in unit]
        recent = [message for unit in recent_units for message in unit]
        summary = self._summarize(
            middle,
            token_budget=max(32, int(self.context_limit * 0.08)),
        )
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

    def _recent_units(
        self,
        units: list[list[dict[str, Any]]],
        *,
        token_budget: int,
    ) -> list[list[dict[str, Any]]]:
        retained: list[list[dict[str, Any]]] = []
        used = 0
        for unit in reversed(units):
            cost = self.estimated_tokens(unit)
            if retained and used + cost > token_budget:
                break
            retained.insert(0, unit)
            used += cost
        return retained

    @staticmethod
    def _message_units(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """Keep an assistant tool request adjacent to every following tool result."""

        units: list[list[dict[str, Any]]] = []
        index = 0
        while index < len(messages):
            message = messages[index]
            unit = [message]
            index += 1
            if message.get("role") == "assistant" and message.get("tool_calls"):
                while index < len(messages) and messages[index].get("role") == "tool":
                    unit.append(messages[index])
                    index += 1
            units.append(unit)
        return units

    @staticmethod
    def _summarize(messages: list[dict[str, Any]], *, token_budget: int) -> str:
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
        rendered = "\n".join(rows) or "- No material messages were removed."
        encoded = rendered.encode("utf-8")
        byte_budget = token_budget * 4
        if len(encoded) <= byte_budget:
            return rendered
        return encoded[:byte_budget].decode("utf-8", errors="ignore") + "\n- ... summary truncated"

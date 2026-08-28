from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_SECRET_TOKEN = re.compile(r"sk-[A-Za-z0-9_-]{12,}")
_TRAILING_SECRET_CANDIDATE = re.compile(r"(?:s|sk|sk-[A-Za-z0-9_-]*)$")


def redact_text(text: str, *, api_key: str = "") -> str:
    """Apply the public-output redaction policy to a complete string."""

    intervals = _redaction_intervals(text, api_key=api_key)
    if not intervals:
        return text

    parts: list[str] = []
    cursor = 0
    marker = _redaction_marker(api_key)
    for start, end in intervals:
        parts.extend((text[cursor:start], marker))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def contains_redactable_secret(text: str, *, api_key: str = "") -> bool:
    """Return whether redaction would remove a credential from ``text``.

    Callers that must reject rather than transform provider-private data cannot safely infer
    this by comparing the redacted result: an attacker-controlled credential can equal a
    presentation marker. Inspecting the source intervals keeps that boundary fail closed.
    """

    return bool(_redaction_intervals(text, api_key=api_key))


def redact_json_value(value: Any, *, api_key: str = "") -> Any:
    """Redact every string in a JSON-like Python value before it is serialized.

    Redacting a serialized blob is insufficient because JSON escapes quotes, backslashes, and
    control characters. Object keys are protected too; if redaction collapses two distinct keys,
    the caller receives a deterministic error instead of silently changing provider semantics.
    """

    if isinstance(value, str):
        return redact_text(value, api_key=api_key)
    if isinstance(value, list):
        return [redact_json_value(item, api_key=api_key) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_json_value(item, api_key=api_key) for item in value)
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        source_keys: dict[Any, Any] = {}
        for key, item in value.items():
            safe_key = (
                redact_text(key, api_key=api_key) if isinstance(key, str) else key
            )
            if safe_key in source_keys and source_keys[safe_key] != key:
                raise ValueError("Redaction collapsed distinct JSON object keys")
            source_keys[safe_key] = key
            redacted[safe_key] = redact_json_value(item, api_key=api_key)
        return redacted
    return value


def _redaction_marker(api_key: str) -> str:
    if not api_key:
        return "[REDACTED]"

    # A marker made only from a non-key, non-token character cannot contain the configured key
    # or join the safe text on either side into a new occurrence. Prefer visible block glyphs;
    # the private-use fallback is deterministic and covers adversarial Unicode credentials.
    for character in ("█", "▓", "▒", "░", "■", "●", "◆", "◼"):
        if character not in api_key:
            return character * 10
    for codepoint in range(0xE000, 0xF900):
        character = chr(codepoint)
        if character not in api_key:
            return character * 10
    raise ValueError("Configured credential exhausts the safe redaction alphabet")


def _redaction_intervals(text: str, *, api_key: str) -> list[tuple[int, int]]:
    intervals = [(match.start(), match.end()) for match in _SECRET_TOKEN.finditer(text)]
    if api_key:
        offset = 0
        while (start := text.find(api_key, offset)) >= 0:
            intervals.append((start, start + len(api_key)))
            offset = start + 1

    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


@dataclass(frozen=True, slots=True)
class JsonStringPrefix:
    value: str | None
    complete: bool = False
    valid: bool = True


def json_string_field_prefix(arguments: str, field: str) -> JsonStringPrefix:
    """Decode the safe prefix of a single-field JSON object's string value.

    Tool arguments arrive as arbitrary fragments. This decoder intentionally accepts an
    incomplete string while refusing malformed escapes and surrogate pairs. Full object/schema
    validation remains the provider and agent's responsibility once the stream terminates.
    """

    opening = re.match(
        rf'\A\s*\{{\s*"{re.escape(field)}"\s*:\s*"',
        arguments,
    )
    if opening is None:
        return JsonStringPrefix(value=None)

    output: list[str] = []
    position = opening.end()
    while position < len(arguments):
        character = arguments[position]
        if character == '"':
            return JsonStringPrefix(value="".join(output), complete=True)
        if ord(character) < 0x20:
            return JsonStringPrefix(value=None, valid=False)
        if character != "\\":
            output.append(character)
            position += 1
            continue

        escape_start = position
        position += 1
        if position >= len(arguments):
            break
        escape = arguments[position]
        simple = {
            '"': '"',
            "\\": "\\",
            "/": "/",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
        }
        if escape in simple:
            output.append(simple[escape])
            position += 1
            continue
        if escape != "u":
            return JsonStringPrefix(value=None, valid=False)
        if position + 5 > len(arguments):
            position = escape_start
            break
        digits = arguments[position + 1 : position + 5]
        if not re.fullmatch(r"[0-9A-Fa-f]{4}", digits):
            return JsonStringPrefix(value=None, valid=False)
        codepoint = int(digits, 16)
        position += 5
        if 0xD800 <= codepoint <= 0xDBFF:
            if position + 6 > len(arguments):
                position = escape_start
                break
            if arguments[position : position + 2] != "\\u":
                return JsonStringPrefix(value=None, valid=False)
            low_digits = arguments[position + 2 : position + 6]
            if not re.fullmatch(r"[0-9A-Fa-f]{4}", low_digits):
                return JsonStringPrefix(value=None, valid=False)
            low = int(low_digits, 16)
            if not 0xDC00 <= low <= 0xDFFF:
                return JsonStringPrefix(value=None, valid=False)
            output.append(chr(0x10000 + ((codepoint - 0xD800) << 10) + low - 0xDC00))
            position += 6
        elif 0xDC00 <= codepoint <= 0xDFFF:
            return JsonStringPrefix(value=None, valid=False)
        else:
            output.append(chr(codepoint))

    return JsonStringPrefix(value="".join(output))


class StableStreamingRedactor:
    """Release only text whose future suffix cannot turn it into a credential."""

    def __init__(self, *, api_key: str = "") -> None:
        self._api_key = api_key
        self._last_input = ""
        self._last_stable = ""

    def update(self, text: str) -> str:
        if not text.startswith(self._last_input):
            raise ValueError("Streaming output must grow monotonically")
        self._last_input = text
        boundary = len(text)

        candidate = _TRAILING_SECRET_CANDIDATE.search(text)
        if candidate is not None:
            boundary = min(boundary, candidate.start())

        if self._api_key:
            maximum = min(len(text), len(self._api_key) - 1)
            for length in range(maximum, 0, -1):
                if text.endswith(self._api_key[:length]):
                    boundary = min(boundary, len(text) - length)
                    break
        boundary = _avoid_split_intervals(
            boundary,
            _redaction_intervals(text, api_key=self._api_key),
        )
        stable = redact_text(text[:boundary], api_key=self._api_key)
        if not stable.startswith(self._last_stable):
            raise ValueError("Streaming redaction lost prefix stability")
        self._last_stable = stable
        return stable

    def finish(self, text: str) -> str:
        if not text.startswith(self._last_input):
            raise ValueError("Streaming output must grow monotonically")
        self._last_input = text
        redacted = redact_text(text, api_key=self._api_key)
        if not redacted.startswith(self._last_stable):
            raise ValueError("Final redaction does not extend the published prefix")
        self._last_stable = redacted
        return redacted


def _avoid_split_intervals(boundary: int, intervals: list[tuple[int, int]]) -> int:
    for start, end in intervals:
        if start < boundary < end:
            boundary = start
    return boundary

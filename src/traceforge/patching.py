from __future__ import annotations

import re
from dataclasses import dataclass, field


class PatchError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class FilePatch:
    old_path: str | None
    new_path: str | None
    hunks: list[Hunk]

    @property
    def path(self) -> str:
        path = self.new_path or self.old_path
        if path is None:
            raise PatchError("Patch has neither an old nor a new path")
        return path


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_unified_diff(text: str) -> list[FilePatch]:
    lines = text.splitlines()
    patches: list[FilePatch] = []
    index = 0
    while index < len(lines):
        if not lines[index].startswith("--- "):
            index += 1
            continue
        old_path = _normalize_header_path(lines[index][4:])
        index += 1
        if index >= len(lines) or not lines[index].startswith("+++ "):
            raise PatchError("A --- header must be followed by a +++ header")
        new_path = _normalize_header_path(lines[index][4:])
        index += 1
        hunks: list[Hunk] = []
        while index < len(lines) and not lines[index].startswith("--- "):
            match = _HUNK_HEADER.match(lines[index])
            if match is None:
                index += 1
                continue
            old_start = int(match.group(1))
            old_count = int(match.group(2) or "1")
            new_start = int(match.group(3))
            new_count = int(match.group(4) or "1")
            index += 1
            hunk_lines: list[str] = []
            while index < len(lines):
                line = lines[index]
                if line.startswith("@@ ") or line.startswith("--- "):
                    break
                if line.startswith("\\ No newline at end of file"):
                    index += 1
                    continue
                if not line or line[0] not in {" ", "+", "-"}:
                    raise PatchError(f"Invalid hunk line: {line!r}")
                hunk_lines.append(line)
                index += 1
            hunks.append(Hunk(old_start, old_count, new_start, new_count, hunk_lines))
        if not hunks:
            raise PatchError(f"Patch for {new_path or old_path} has no hunks")
        patches.append(FilePatch(old_path, new_path, hunks))
    if not patches:
        raise PatchError("No unified diff file headers found")
    return patches


def apply_file_patch(original: str, patch: FilePatch) -> str:
    old_lines = original.splitlines()
    trailing_newline = original.endswith("\n")
    result: list[str] = []
    cursor = 0
    for hunk in patch.hunks:
        target = max(hunk.old_start - 1, 0)
        if target < cursor or target > len(old_lines):
            raise PatchError(f"Hunk starts outside the source at line {hunk.old_start}")
        result.extend(old_lines[cursor:target])
        cursor = target
        consumed = 0
        produced = 0
        for line in hunk.lines:
            marker, value = line[0], line[1:]
            if marker in {" ", "-"}:
                if cursor >= len(old_lines) or old_lines[cursor] != value:
                    actual = old_lines[cursor] if cursor < len(old_lines) else "<eof>"
                    raise PatchError(
                        f"Patch context mismatch at source line {cursor + 1}: "
                        f"expected {value!r}, found {actual!r}"
                    )
                cursor += 1
                consumed += 1
            if marker in {" ", "+"}:
                result.append(value)
                produced += 1
        if consumed != hunk.old_count:
            raise PatchError(
                f"Hunk consumed {consumed} lines but header declares {hunk.old_count}"
            )
        if produced != hunk.new_count:
            raise PatchError(
                f"Hunk produced {produced} lines but header declares {hunk.new_count}"
            )
    result.extend(old_lines[cursor:])
    rendered = "\n".join(result)
    if patch.new_path is not None and (trailing_newline or result):
        rendered += "\n"
    return rendered


def _normalize_header_path(raw: str) -> str | None:
    value = raw.split("\t", 1)[0].strip()
    if value == "/dev/null":
        return None
    if value.startswith(("a/", "b/")):
        value = value[2:]
    if not value:
        raise PatchError("Patch path is empty")
    return value


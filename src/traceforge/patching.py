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
_APPLY_PATCH_FILE = re.compile(r"^\*\*\* (Update|Add|Delete) File: (.+)$")


def parse_unified_diff(text: str) -> list[FilePatch]:
    lines = _normalize_apply_patch_format(text).splitlines()
    patches: list[FilePatch] = []
    index = 0
    while index < len(lines):
        if not _is_file_header(lines, index):
            index += 1
            continue
        old_path = _normalize_header_path(lines[index][4:])
        index += 1
        if index >= len(lines) or not lines[index].startswith("+++ "):
            raise PatchError("A --- header must be followed by a +++ header")
        new_path = _normalize_header_path(lines[index][4:])
        index += 1
        hunks: list[Hunk] = []
        while index < len(lines) and not _is_file_header(lines, index):
            match = _HUNK_HEADER.match(lines[index])
            if match is None:
                index += 1
                continue
            old_start = int(match.group(1))
            new_start = int(match.group(3))
            index += 1
            hunk_lines: list[str] = []
            while index < len(lines):
                line = lines[index]
                if line.startswith("@@ ") or _is_file_header(lines, index):
                    break
                if line.startswith("\\ No newline at end of file"):
                    index += 1
                    continue
                if not line:
                    line = " "
                if line[0] not in {" ", "+", "-"}:
                    raise PatchError(f"Invalid hunk line: {line!r}")
                hunk_lines.append(line)
                index += 1
            actual_old_count = sum(line[0] in {" ", "-"} for line in hunk_lines)
            actual_new_count = sum(line[0] in {" ", "+"} for line in hunk_lines)
            hunks.append(
                Hunk(old_start, actual_old_count, new_start, actual_new_count, hunk_lines)
            )
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
        target = _locate_hunk(old_lines, hunk, cursor)
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


def _normalize_apply_patch_format(text: str) -> str:
    lines = text.splitlines()
    has_apply_patch_envelope = any(
        line in {"*** Begin Patch", "*** End Patch"} for line in lines
    )
    has_file_markers = any(_APPLY_PATCH_FILE.match(line) for line in lines)
    if not has_apply_patch_envelope and not has_file_markers:
        return text

    cleaned = [
        line
        for line in lines
        if line not in {"*** Begin Patch", "*** End Patch", "*** End of File"}
    ]
    if not has_file_markers:
        return "\n".join(cleaned)

    sections: list[tuple[str, str, list[str]]] = []
    current: tuple[str, str, list[str]] | None = None
    for line in cleaned:
        match = _APPLY_PATCH_FILE.match(line)
        if match:
            if current is not None:
                sections.append(current)
            current = (match.group(1), match.group(2).strip(), [])
            continue
        if line.startswith("*** Move to:"):
            raise PatchError("File renames are not supported")
        if current is None:
            if line.strip():
                raise PatchError(f"Unexpected line before file marker: {line!r}")
            continue
        current[2].append(line)
    if current is not None:
        sections.append(current)

    rendered: list[str] = []
    for kind, path, body in sections:
        if not path:
            raise PatchError("Patch path is empty")
        if kind == "Add":
            rendered.extend(["--- /dev/null", f"+++ b/{path}"])
        elif kind == "Delete":
            rendered.extend([f"--- a/{path}", "+++ /dev/null"])
        else:
            rendered.extend([f"--- a/{path}", f"+++ b/{path}"])

        if not any(line.startswith("@@") for line in body):
            new_start = 1 if kind == "Add" else 0
            rendered.append(f"@@ -0,0 +{new_start},0 @@")
        for line in body:
            if line.startswith("@@") and _HUNK_HEADER.match(line) is None:
                rendered.append("@@ -0,0 +0,0 @@")
            else:
                rendered.append(line)
    return "\n".join(rendered)


def _is_file_header(lines: list[str], index: int) -> bool:
    return (
        index + 1 < len(lines)
        and lines[index].startswith("--- ")
        and lines[index + 1].startswith("+++ ")
    )


def _locate_hunk(old_lines: list[str], hunk: Hunk, cursor: int) -> int:
    if hunk.old_start != 0 or hunk.old_count == 0:
        return max(hunk.old_start - 1, 0)

    expected = [line[1:] for line in hunk.lines if line[0] in {" ", "-"}]
    candidates = [
        start
        for start in range(cursor, len(old_lines) - len(expected) + 1)
        if old_lines[start : start + len(expected)] == expected
    ]
    if not candidates:
        raise PatchError("Could not locate the patch context in the source file")
    if len(candidates) > 1:
        raise PatchError("Patch context is ambiguous; include more surrounding lines")
    return candidates[0]


def _normalize_header_path(raw: str) -> str | None:
    value = raw.split("\t", 1)[0].strip()
    if value == "/dev/null":
        return None
    if value.startswith(("a/", "b/")):
        value = value[2:]
    if not value:
        raise PatchError("Patch path is empty")
    return value

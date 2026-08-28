from __future__ import annotations

import difflib
import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from traceforge.storage import SnapshotRecord, Storage


class WorkspaceViolation(ValueError):
    """Raised when a requested path escapes the selected workspace."""


@dataclass(slots=True)
class RollbackResult:
    restored: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


class Workspace:
    def __init__(self, root: Path, storage: Storage) -> None:
        self.root = root.expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise WorkspaceViolation(f"Workspace is not a directory: {self.root}")
        self.storage = storage

    def resolve_read(self, relative_path: str) -> Path:
        candidate = self._candidate(relative_path)
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise WorkspaceViolation(f"Path does not exist: {relative_path}") from exc
        self._ensure_within(resolved)
        return resolved

    def resolve_write(self, relative_path: str, *, must_exist: bool | None = None) -> Path:
        candidate = self._candidate(relative_path)
        ancestor = candidate.parent
        while not ancestor.exists() and ancestor != self.root:
            ancestor = ancestor.parent
        resolved_ancestor = ancestor.resolve(strict=True)
        self._ensure_within(resolved_ancestor)
        self._reject_symlink_components(candidate)
        exists = candidate.exists()
        if must_exist is True and not exists:
            raise WorkspaceViolation(f"Path does not exist: {relative_path}")
        if must_exist is False and exists:
            raise WorkspaceViolation(f"Path already exists: {relative_path}")
        if exists:
            resolved = candidate.resolve(strict=True)
            self._ensure_within(resolved)
            if not resolved.is_file():
                raise WorkspaceViolation(f"Path is not a regular file: {relative_path}")
        return candidate

    def relative(self, path: Path) -> str:
        resolved = path.resolve(strict=False)
        self._ensure_within(resolved)
        return resolved.relative_to(self.root).as_posix()

    def snapshot(self, run_id: str, path: Path) -> None:
        relative_path = self.relative(path)
        if path.exists():
            content = path.read_bytes()
            mode = stat.S_IMODE(path.stat().st_mode)
            original_hash = digest(content)
            existed = True
        else:
            content = None
            mode = None
            original_hash = None
            existed = False
        self.storage.save_snapshot_if_absent(
            SnapshotRecord(
                run_id=run_id,
                path=relative_path,
                existed=existed,
                content=content,
                mode=mode,
                original_hash=original_hash,
                last_agent_hash=original_hash,
            )
        )

    def record_agent_version(self, run_id: str, path: Path) -> None:
        relative_path = self.relative(path)
        last_hash = digest(path.read_bytes()) if path.exists() else None
        self.storage.update_snapshot_hash(run_id, relative_path, last_hash)

    def rollback(self, run_id: str) -> RollbackResult:
        result = RollbackResult()
        for snapshot in self.storage.list_snapshots(run_id):
            path = self.resolve_write(snapshot.path)
            current_hash = digest(path.read_bytes()) if path.exists() else None
            if snapshot.existed and current_hash == snapshot.original_hash:
                if snapshot.mode is not None:
                    os.chmod(path, snapshot.mode)
                result.restored.append(snapshot.path)
                continue
            if not snapshot.existed and not path.exists():
                result.removed.append(snapshot.path)
                continue
            if current_hash != snapshot.last_agent_hash:
                result.conflicts.append(snapshot.path)
                continue
            if snapshot.existed:
                assert snapshot.content is not None
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(snapshot.content)
                if snapshot.mode is not None:
                    os.chmod(path, snapshot.mode)
                result.restored.append(snapshot.path)
            elif path.exists():
                path.unlink()
                self._prune_empty_parents(path.parent)
                result.removed.append(snapshot.path)
        return result

    def diff(self, run_id: str) -> str:
        sections: list[str] = []
        for snapshot in self.storage.list_snapshots(run_id):
            path = self.resolve_write(snapshot.path)
            original = snapshot.content or b""
            current = path.read_bytes() if path.exists() else b""
            if original == current:
                continue
            if b"\0" in original or b"\0" in current:
                sections.append(f"Binary file changed: {snapshot.path}\n")
                continue
            old_lines = original.decode("utf-8", errors="replace").splitlines(keepends=True)
            new_lines = current.decode("utf-8", errors="replace").splitlines(keepends=True)
            old_name = f"a/{snapshot.path}" if snapshot.existed else "/dev/null"
            new_name = f"b/{snapshot.path}" if path.exists() else "/dev/null"
            sections.extend(
                difflib.unified_diff(old_lines, new_lines, fromfile=old_name, tofile=new_name)
            )
        return "".join(sections)

    def _candidate(self, relative_path: str) -> Path:
        if not relative_path or "\x00" in relative_path:
            raise WorkspaceViolation("Path must be a non-empty relative path")
        supplied = Path(relative_path)
        if supplied.is_absolute():
            raise WorkspaceViolation("Absolute paths are not allowed")
        if any(part in {"", ".git"} for part in supplied.parts):
            raise WorkspaceViolation("Writing .git or empty path components is not allowed")
        candidate = self.root / supplied
        self._ensure_within(candidate.resolve(strict=False))
        return candidate

    def _ensure_within(self, path: Path) -> None:
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceViolation(f"Path escapes workspace: {path}") from exc

    def _reject_symlink_components(self, candidate: Path) -> None:
        relative = candidate.relative_to(self.root)
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise WorkspaceViolation(f"Writes through symlinks are not allowed: {relative}")

    def _prune_empty_parents(self, directory: Path) -> None:
        current = directory
        while current != self.root:
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()

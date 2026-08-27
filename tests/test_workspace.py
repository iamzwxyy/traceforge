from __future__ import annotations

import os
from pathlib import Path

import pytest

from traceforge.models import RunRecord
from traceforge.storage import Storage
from traceforge.workspace import Workspace, WorkspaceViolation


def _create_run(storage: Storage, workspace: Workspace, run_id: str = "run-1") -> None:
    storage.create_run(RunRecord(id=run_id, task="Edit", workspace=str(workspace.root)))


def test_path_traversal_and_git_are_rejected(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceViolation, match="escapes"):
        workspace.resolve_write("../outside.txt")
    with pytest.raises(WorkspaceViolation, match=r"\.git"):
        workspace.resolve_write(".git/config")


def test_symlink_escape_is_rejected(workspace: Workspace, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace.root / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspaceViolation, match=r"escapes|symlink"):
        workspace.resolve_write("link/secret.txt")


def test_rollback_restores_and_removes_files(workspace: Workspace, storage: Storage) -> None:
    _create_run(storage, workspace)
    existing = workspace.root / "existing.txt"
    existing.write_text("before\n")
    new_file = workspace.root / "nested" / "new.txt"

    workspace.snapshot("run-1", existing)
    existing.write_text("after\n")
    workspace.record_agent_version("run-1", existing)

    workspace.snapshot("run-1", new_file)
    new_file.parent.mkdir()
    new_file.write_text("new\n")
    workspace.record_agent_version("run-1", new_file)

    result = workspace.rollback("run-1")

    assert existing.read_text() == "before\n"
    assert not new_file.exists()
    assert not new_file.parent.exists()
    assert result.restored == ["existing.txt"]
    assert result.removed == ["nested/new.txt"]


def test_rollback_skips_user_modified_file(workspace: Workspace, storage: Storage) -> None:
    _create_run(storage, workspace)
    target = workspace.root / "file.txt"
    target.write_text("before\n")
    workspace.snapshot("run-1", target)
    target.write_text("agent\n")
    workspace.record_agent_version("run-1", target)
    target.write_text("user\n")

    result = workspace.rollback("run-1")

    assert target.read_text() == "user\n"
    assert result.conflicts == ["file.txt"]


def test_rollback_restores_safe_files_while_preserving_one_conflict(
    workspace: Workspace, storage: Storage
) -> None:
    _create_run(storage, workspace)
    safe = workspace.root / "safe.txt"
    conflicted = workspace.root / "conflicted.txt"
    safe.write_text("before safe\n")
    conflicted.write_text("before conflict\n")
    for target in (safe, conflicted):
        workspace.snapshot("run-1", target)
        target.write_text("agent\n")
        workspace.record_agent_version("run-1", target)
    conflicted.write_text("user keeps this\n")

    result = workspace.rollback("run-1")

    assert safe.read_text() == "before safe\n"
    assert conflicted.read_text() == "user keeps this\n"
    assert result.restored == ["safe.txt"]
    assert result.conflicts == ["conflicted.txt"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode test")
def test_rollback_restores_permissions(workspace: Workspace, storage: Storage) -> None:
    _create_run(storage, workspace)
    target = workspace.root / "script.sh"
    target.write_text("echo before\n")
    target.chmod(0o744)
    workspace.snapshot("run-1", target)
    target.write_text("echo after\n")
    target.chmod(0o600)
    workspace.record_agent_version("run-1", target)

    workspace.rollback("run-1")

    assert target.stat().st_mode & 0o777 == 0o744

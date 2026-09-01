from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from traceforge.config import Settings
from traceforge.models import RunRecord, ToolCall, WorkspaceInstructionSnapshot
from traceforge.sandbox import CommandSandbox, SandboxStatus
from traceforge.storage import Storage
from traceforge.tools import ToolRegistry
from traceforge.workspace import Workspace


def _persisted_registry(
    storage: Storage,
    workspace: Workspace,
    settings: Settings,
    run: RunRecord,
) -> ToolRegistry:
    snapshot = WorkspaceInstructionSnapshot.empty()
    storage.create_run(run, instruction_snapshot=snapshot)
    registry = ToolRegistry(workspace, settings)
    registry.bind_workspace_instruction_snapshot(run.id, snapshot.snapshot_sha256)
    return registry


def test_seatbelt_launch_is_parameterized_and_fail_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command_temp = tmp_path / "command-temp"
    command_temp.mkdir()
    sandbox = CommandSandbox(workspace)
    sandbox.status = SandboxStatus("seatbelt", True, "test backend")
    sandbox._program = "/usr/bin/sandbox-exec"

    launch = sandbox.prepare(
        "/usr/bin/python3",
        ["python3", "-c", "print('ok')"],
        cwd=workspace,
        command_temp=command_temp,
        environment={"PATH": "/usr/bin:/bin"},
        bypass=False,
    )

    assert launch.program == "/usr/bin/sandbox-exec"
    assert launch.arguments[0] == "-p"
    profile = launch.arguments[1]
    assert "deny network-outbound" in profile
    assert "deny file-write*" in profile
    assert "deny file-read-data" in profile
    assert f"-DWORKSPACE={workspace}" in launch.arguments
    assert launch.arguments[-4:] == ["--", "/usr/bin/python3", "-c", "print('ok')"]
    assert launch.metadata["status"] == "enforced"


def test_bubblewrap_launch_is_namespaced_and_keeps_git_read_only(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    git_dir = workspace / ".git"
    git_dir.mkdir()
    (workspace / ".env").write_text("SECRET=masked\n")
    command_temp = tmp_path / "command-temp"
    command_temp.mkdir()
    sandbox = CommandSandbox(workspace)
    sandbox.status = SandboxStatus("bubblewrap", True, "test backend")
    sandbox._program = "/usr/bin/bwrap"

    launch = sandbox.prepare(
        "/usr/bin/python3",
        ["python3", "-V"],
        cwd=workspace,
        command_temp=command_temp,
        environment={"PATH": "/usr/bin:/bin"},
        bypass=False,
    )

    arguments = launch.arguments
    assert launch.program == "/usr/bin/bwrap"
    assert {"--unshare-user", "--unshare-pid", "--unshare-net", "--new-session"} <= set(
        arguments
    )
    workspace_bind = arguments.index(str(workspace))
    assert arguments[workspace_bind - 1 : workspace_bind + 2] == [
        "--bind",
        str(workspace),
        str(workspace),
    ]
    git_bind = arguments.index(str(git_dir))
    assert arguments[git_bind - 1 : git_bind + 2] == [
        "--ro-bind",
        str(git_dir),
        str(git_dir),
    ]
    assert str(workspace / ".env") in arguments
    assert launch.metadata["network"] == "isolated_namespace"


def test_seatbelt_scoped_launch_narrows_command_root_and_rejects_bypass(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "alpha"
    sibling = workspace / "beta"
    project.mkdir(parents=True)
    sibling.mkdir()
    command_temp = tmp_path / "command-temp"
    command_temp.mkdir()
    sandbox = CommandSandbox(workspace)
    sandbox.status = SandboxStatus("seatbelt", True, "test backend")
    sandbox._program = "/usr/bin/sandbox-exec"

    launch = sandbox.prepare(
        "/usr/bin/python3",
        ["python3", "-V"],
        cwd=project,
        command_temp=command_temp,
        environment={"PATH": "/usr/bin:/bin"},
        bypass=False,
        execution_root=project,
    )

    profile = launch.arguments[1]
    assert f"-DCOMMAND_ROOT={project}" in launch.arguments
    assert '(require-not (subpath (param "COMMAND_ROOT")))' in profile
    assert (
        "(deny file-read-data (require-all "
        '(subpath (param "WORKSPACE")) '
        '(require-not (subpath (param "COMMAND_ROOT")))))'
    ) in profile
    assert launch.metadata["command_root"] == str(project)
    with pytest.raises(ValueError, match="cwd must stay inside"):
        sandbox.prepare(
            "/usr/bin/python3",
            ["python3", "-V"],
            cwd=sibling,
            command_temp=command_temp,
            environment={"PATH": "/usr/bin:/bin"},
            bypass=False,
            execution_root=project,
        )
    approved = sandbox.prepare(
        "/usr/bin/python3",
        ["python3", "-V"],
        cwd=project,
        command_temp=command_temp,
        environment={"PATH": "/usr/bin:/bin"},
        bypass=True,
        execution_root=project,
    )
    assert approved.program == "/usr/bin/sandbox-exec"
    assert approved.metadata["status"] == "enforced"
    assert approved.metadata["scope_enforced"] is True
    assert approved.metadata["bypass_requested"] is True


def test_bubblewrap_scoped_launch_binds_only_project_writable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "alpha"
    workspace_git = workspace / ".git"
    project_git = project / ".git"
    workspace_git.mkdir(parents=True)
    project_git.mkdir(parents=True)
    (workspace / "beta").mkdir()
    command_temp = tmp_path / "command-temp"
    command_temp.mkdir()
    sandbox = CommandSandbox(workspace)
    sandbox.status = SandboxStatus("bubblewrap", True, "test backend")
    sandbox._program = "/usr/bin/bwrap"

    launch = sandbox.prepare(
        "/usr/bin/python3",
        ["python3", "-V"],
        cwd=project,
        command_temp=command_temp,
        environment={"PATH": "/usr/bin:/bin"},
        bypass=False,
        execution_root=project,
    )

    arguments = launch.arguments
    project_bind = ["--bind", str(project), str(project)]
    assert any(
        arguments[index : index + 3] == project_bind
        for index in range(len(arguments) - 2)
    )
    workspace_mask = arguments.index(str(workspace))
    project_mount = next(
        index
        for index in range(len(arguments) - 2)
        if arguments[index : index + 3] == project_bind
    )
    assert arguments[workspace_mask - 1 : workspace_mask + 1] == [
        "--tmpfs",
        str(workspace),
    ]
    assert workspace_mask < project_mount
    assert not any(
        arguments[index : index + 3]
        == ["--bind", str(workspace), str(workspace)]
        for index in range(len(arguments) - 2)
    )
    git_index = arguments.index(str(project_git))
    assert arguments[git_index - 1 : git_index + 2] == [
        "--ro-bind",
        str(project_git),
        str(project_git),
    ]
    assert str(workspace_git) not in arguments


def test_policy_only_sandbox_fails_closed_for_scoped_commands(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "alpha"
    project.mkdir(parents=True)
    command_temp = tmp_path / "command-temp"
    command_temp.mkdir()
    sandbox = CommandSandbox(workspace)
    sandbox.status = SandboxStatus("none", False, "test backend unavailable")
    sandbox._program = None

    with pytest.raises(ValueError, match="require an enforced OS sandbox"):
        sandbox.prepare(
            "/usr/bin/python3",
            ["python3", "-V"],
            cwd=project,
            command_temp=command_temp,
            environment={"PATH": "/usr/bin:/bin"},
            bypass=False,
            execution_root=project,
        )


@pytest.mark.asyncio
async def test_enforced_sandbox_allows_workspace_write_and_blocks_escape(
    settings: Settings, storage: Storage, workspace: Workspace
) -> None:
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="sandbox-run", task="Probe", workspace=str(workspace.root)),
    )
    if not registry.sandbox_status.enforced:
        pytest.skip(registry.sandbox_status.detail)
    outside = workspace.root.parent / "sandbox-escape.txt"
    outside.unlink(missing_ok=True)

    inside_result = await registry.execute(
        "sandbox-run",
        ToolCall(
            id="inside",
            name="run_command",
            arguments={
                "argv": [
                    "python3",
                    "-c",
                    "from pathlib import Path; Path('inside.txt').write_text('allowed')",
                ]
            },
        ),
    )
    escape_result = await registry.execute(
        "sandbox-run",
        ToolCall(
            id="escape",
            name="run_command",
            arguments={
                "argv": [
                    "python3",
                    "-c",
                    f"from pathlib import Path; Path({str(outside)!r}).write_text('escaped')",
                ]
            },
        ),
    )

    assert inside_result.ok
    assert (workspace.root / "inside.txt").read_text() == "allowed"
    assert inside_result.metadata["sandbox"]["status"] == "enforced"
    assert not escape_result.ok
    assert escape_result.metadata["sandbox"]["status"] == "enforced"
    assert not outside.exists()


@pytest.mark.asyncio
async def test_enforced_scoped_command_writes_project_but_not_sibling_even_when_approved(
    settings: Settings,
    storage: Storage,
    workspace: Workspace,
) -> None:
    alpha = workspace.root / "alpha"
    beta = workspace.root / "beta"
    alpha.mkdir()
    beta.mkdir()
    sensitive_content = "sibling project contents must stay private"
    (beta / "secret.txt").write_text(sensitive_content)
    (alpha / "sibling-link").symlink_to(beta, target_is_directory=True)
    run_id = "sandbox-project-scope"
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id=run_id, task="Probe alpha", workspace=str(workspace.root)),
    )
    registry.bind_project_scope(run_id, "alpha")
    if not registry.sandbox_status.enforced:
        pytest.skip(registry.sandbox_status.detail)

    inside = await registry.execute(
        run_id,
        ToolCall(
            id="inside-project",
            name="run_command",
            arguments={
                "argv": [
                    "python3",
                    "-c",
                    "from pathlib import Path; Path('inside.txt').write_text('allowed')",
                ]
            },
        ),
    )
    sibling = await registry.execute(
        run_id,
        ToolCall(
            id="sibling-escape",
            name="run_command",
            arguments={
                "argv": [
                    "python3",
                    "-c",
                    (
                        "from pathlib import Path; "
                        "Path('../beta/escape.txt').write_text('escaped')"
                    ),
                ]
            },
        ),
        sandbox_bypass=True,
    )
    sibling_read = await registry.execute(
        run_id,
        ToolCall(
            id="sibling-read",
            name="run_command",
            arguments={
                "argv": [
                    "python3",
                    "-c",
                    "from pathlib import Path; print(Path('../beta/secret.txt').read_text())",
                ]
            },
        ),
        sandbox_bypass=True,
    )
    symlink_read = await registry.execute(
        run_id,
        ToolCall(
            id="sibling-symlink-read",
            name="run_command",
            arguments={
                "argv": [
                    "python3",
                    "-c",
                    "from pathlib import Path; print(Path('sibling-link/secret.txt').read_text())",
                ]
            },
        ),
        sandbox_bypass=True,
    )

    assert inside.ok, inside.output
    assert (alpha / "inside.txt").read_text() == "allowed"
    assert inside.metadata["cwd"] == "alpha"
    assert not sibling.ok
    assert sibling.metadata["sandbox"]["status"] == "enforced"
    assert sibling.metadata["sandbox"]["bypass_requested"] is True
    assert not (beta / "escape.txt").exists()
    assert not sibling_read.ok
    assert sibling_read.metadata["sandbox"]["scope_enforced"] is True
    assert sensitive_content not in sibling_read.output
    assert not symlink_read.ok
    assert symlink_read.metadata["sandbox"]["scope_enforced"] is True
    assert sensitive_content not in symlink_read.output


@pytest.mark.asyncio
async def test_enforced_sandbox_allows_isolated_command_cache_writes(
    settings: Settings, storage: Storage, workspace: Workspace
) -> None:
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="cache-run", task="Probe cache", workspace=str(workspace.root)),
    )
    if not registry.sandbox_status.enforced:
        pytest.skip(registry.sandbox_status.detail)

    result = await registry.execute(
        "cache-run",
        ToolCall(
            id="cache",
            name="run_command",
            arguments={
                "argv": [
                    "python3",
                    "-c",
                    (
                        "import os; from pathlib import Path; "
                        "cache = Path(os.environ['UV_CACHE_DIR']); "
                        "cache.mkdir(parents=True); (cache / 'probe').write_text('ok')"
                    ),
                ]
            },
        ),
    )

    assert result.ok, result.output
    assert result.metadata["sandbox"]["status"] == "enforced"


@pytest.mark.asyncio
async def test_enforced_sandbox_blocks_secret_file_contents(
    settings: Settings, storage: Storage, workspace: Workspace
) -> None:
    secret = workspace.root / ".env"
    secret.write_text("TRACEFORGE_SANDBOX_SECRET=never-readable\n")
    configured = replace(settings, credential_file=secret)
    registry = _persisted_registry(
        storage,
        workspace,
        configured,
        RunRecord(id="secret-run", task="Probe", workspace=str(workspace.root)),
    )
    if not registry.sandbox_status.enforced:
        pytest.skip(registry.sandbox_status.detail)

    result = await registry.execute(
        "secret-run",
        ToolCall(
            id="secret",
            name="run_command",
            arguments={
                "argv": [
                    "python3",
                    "-c",
                    "from pathlib import Path; print(Path('.env').read_text())",
                ]
            },
        ),
    )

    assert not result.ok
    assert "never-readable" not in result.output
    assert result.metadata["sandbox"]["status"] == "enforced"


@pytest.mark.asyncio
async def test_explicit_sandbox_bypass_is_visible_and_one_shot(
    settings: Settings, storage: Storage, workspace: Workspace
) -> None:
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="bypass-run", task="Probe", workspace=str(workspace.root)),
    )
    outside = workspace.root.parent / "approved-bypass.txt"
    outside.unlink(missing_ok=True)

    result = await registry.execute(
        "bypass-run",
        ToolCall(
            id="bypass",
            name="run_command",
            arguments={
                "argv": [
                    "python3",
                    "-c",
                    f"from pathlib import Path; Path({str(outside)!r}).write_text('approved')",
                ]
            },
        ),
        sandbox_bypass=True,
    )

    assert result.ok
    assert outside.read_text() == "approved"
    assert result.metadata["sandbox"]["status"] == "bypassed"
    outside.unlink()

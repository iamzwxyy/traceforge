from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from traceforge.config import Settings
from traceforge.models import RunRecord, ToolCall
from traceforge.sandbox import CommandSandbox, SandboxStatus
from traceforge.storage import Storage
from traceforge.tools import ToolRegistry
from traceforge.workspace import Workspace


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


@pytest.mark.asyncio
async def test_enforced_sandbox_allows_workspace_write_and_blocks_escape(
    settings: Settings, storage: Storage, workspace: Workspace
) -> None:
    registry = ToolRegistry(workspace, settings)
    if not registry.sandbox_status.enforced:
        pytest.skip(registry.sandbox_status.detail)
    storage.create_run(RunRecord(id="sandbox-run", task="Probe", workspace=str(workspace.root)))
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
async def test_enforced_sandbox_blocks_secret_file_contents(
    settings: Settings, storage: Storage, workspace: Workspace
) -> None:
    secret = workspace.root / ".env"
    secret.write_text("TRACEFORGE_SANDBOX_SECRET=never-readable\n")
    configured = replace(settings, credential_file=secret)
    registry = ToolRegistry(workspace, configured)
    if not registry.sandbox_status.enforced:
        pytest.skip(registry.sandbox_status.detail)
    storage.create_run(RunRecord(id="secret-run", task="Probe", workspace=str(workspace.root)))

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
    registry = ToolRegistry(workspace, settings)
    storage.create_run(RunRecord(id="bypass-run", task="Probe", workspace=str(workspace.root)))
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

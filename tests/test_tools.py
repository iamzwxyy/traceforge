from __future__ import annotations

from pathlib import Path

import pytest

from traceforge.config import Settings
from traceforge.models import AcceptanceCheck, PlanStep, RunRecord, TaskPlan, ToolCall
from traceforge.storage import Storage
from traceforge.tools import PermissionDecision, ToolRegistry
from traceforge.workspace import Workspace


def _plan(command: list[str]) -> TaskPlan:
    return TaskPlan(
        summary="Make the tests pass",
        steps=[PlanStep(id="fix", title="Fix")],
        acceptance_checks=[AcceptanceCheck(id="test", label="Tests", command=command)],
    )


def test_permission_policy(settings: Settings, workspace: Workspace) -> None:
    registry = ToolRegistry(workspace, settings)
    read_call = ToolCall(id="1", name="run_command", arguments={"argv": ["git", "status"]})
    test_call = ToolCall(id="2", name="run_command", arguments={"argv": ["uv", "run", "pytest"]})
    unknown_call = ToolCall(id="3", name="run_command", arguments={"argv": ["python", "app.py"]})
    denied_call = ToolCall(id="4", name="run_command", arguments={"argv": ["sudo", "reboot"]})

    assert registry.assess(read_call, None).decision is PermissionDecision.ALLOW
    assert registry.assess(test_call, _plan(["uv", "run", "pytest"])).decision is (
        PermissionDecision.ALLOW
    )
    assert registry.assess(unknown_call, None).decision is PermissionDecision.ASK
    assert registry.assess(denied_call, None).decision is PermissionDecision.DENY


@pytest.mark.asyncio
async def test_create_patch_command_and_rollback(
    settings: Settings, workspace: Workspace, storage: Storage
) -> None:
    storage.create_run(RunRecord(id="run-1", task="Edit", workspace=str(workspace.root)))
    registry = ToolRegistry(workspace, settings)

    create = await registry.execute(
        "run-1",
        ToolCall(
            id="create",
            name="create_file",
            arguments={"path": "src/example.py", "content": "value = 1\n"},
        ),
    )
    patch = await registry.execute(
        "run-1",
        ToolCall(
            id="patch",
            name="apply_patch",
            arguments={
                "patch": (
                    "--- a/src/example.py\n"
                    "+++ b/src/example.py\n"
                    "@@ -1 +1 @@\n"
                    "-value = 1\n"
                    "+value = 2\n"
                )
            },
        ),
    )
    command = await registry.execute(
        "run-1",
        ToolCall(
            id="command",
            name="run_command",
            arguments={"argv": ["python3", "-c", "print('verified')"]},
        ),
    )

    assert create.ok and patch.ok and command.ok
    assert (workspace.root / "src/example.py").read_text() == "value = 2\n"
    assert "verified" in command.output
    workspace.rollback("run-1")
    assert not (workspace.root / "src/example.py").exists()


@pytest.mark.asyncio
async def test_command_timeout(
    settings: Settings, workspace: Workspace, storage: Storage
) -> None:
    storage.create_run(RunRecord(id="run-1", task="Wait", workspace=str(workspace.root)))
    registry = ToolRegistry(workspace, settings)
    result = await registry.execute(
        "run-1",
        ToolCall(
            id="slow",
            name="run_command",
            arguments={
                "argv": ["python3", "-c", "import time; time.sleep(2)"],
                "timeout_seconds": 1,
            },
        ),
    )
    assert not result.ok
    assert result.metadata["timeout"] is True


def test_diff_is_generated_for_modified_file(
    settings: Settings, workspace: Workspace, storage: Storage
) -> None:
    storage.create_run(RunRecord(id="run-1", task="Edit", workspace=str(workspace.root)))
    target = Path(settings.workspace) / "file.txt"
    target.write_text("old\n")
    workspace.snapshot("run-1", target)
    target.write_text("new\n")
    workspace.record_agent_version("run-1", target)

    assert "-old" in workspace.diff("run-1")
    assert "+new" in workspace.diff("run-1")


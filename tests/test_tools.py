from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import traceforge.tools as tools_module
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


def test_permission_policy_enforces_visible_mutation_scope(
    settings: Settings, workspace: Workspace
) -> None:
    registry = ToolRegistry(workspace, settings)
    plan = _plan(["uv", "run", "pytest"]).model_copy(
        update={"impacted_files": ["src/allowed.py"]}
    )
    allowed = ToolCall(
        id="allowed",
        name="create_file",
        arguments={"path": "src/allowed.py", "content": "value = 1\n"},
    )
    drift = ToolCall(
        id="drift",
        name="create_file",
        arguments={"path": "src/unplanned.py", "content": "value = 2\n"},
    )
    multi_file_patch = ToolCall(
        id="patch",
        name="apply_patch",
        arguments={
            "patch": (
                "--- a/src/allowed.py\n"
                "+++ b/src/allowed.py\n"
                "@@ -1 +1 @@\n"
                "-value = 1\n"
                "+value = 2\n"
                "--- a/src/unplanned.py\n"
                "+++ b/src/unplanned.py\n"
                "@@ -1 +1 @@\n"
                "-value = 1\n"
                "+value = 2\n"
            )
        },
    )

    assert registry.assess(allowed, plan).decision is PermissionDecision.ALLOW
    drift_assessment = registry.assess(drift, plan)
    assert drift_assessment.decision is PermissionDecision.ASK
    assert "src/unplanned.py" in drift_assessment.reason
    patch_assessment = registry.assess(multi_file_patch, plan)
    assert patch_assessment.decision is PermissionDecision.ASK
    assert "src/unplanned.py" in patch_assessment.reason


def test_permission_policy_denies_malformed_and_destructive_commands(
    settings: Settings, workspace: Workspace
) -> None:
    registry = ToolRegistry(workspace, settings)

    for arguments in ({"argv": []}, {"argv": "git status"}, {"argv": ["git", 1]}):
        call = ToolCall(id="bad", name="run_command", arguments=arguments)
        assert registry.assess(call, None).decision is PermissionDecision.DENY
    for argv in (
        ["git", "reset", "--hard"],
        ["rm", "-rf", "target"],
        ["find", "/", "-name", "python"],
        ["cat", "../../outside.txt"],
        ["tool", "--config=/tmp/outside.toml"],
    ):
        call = ToolCall(id="danger", name="run_command", arguments={"argv": argv})
        assert registry.assess(call, None).decision is PermissionDecision.DENY
    assert registry.assess(
        ToolCall(id="read", name="run_command", arguments={"argv": ["ls"]}), None
    ).decision is PermissionDecision.ALLOW


@pytest.mark.asyncio
async def test_command_prefers_traceforge_runtime_and_rejects_external_paths(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.create_run(RunRecord(id="run-1", task="Run", workspace=str(workspace.root)))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    registry = ToolRegistry(workspace, settings)

    runtime = await registry.execute(
        "run-1",
        ToolCall(
            id="runtime",
            name="run_command",
            arguments={"argv": ["python", "-c", "import sys; print(sys.executable)"]},
        ),
    )
    external = await registry.execute(
        "run-1",
        ToolCall(
            id="external",
            name="run_command",
            arguments={"argv": ["find", "/", "-name", "python"]},
        ),
    )

    assert runtime.ok
    assert runtime.output.strip() == tools_module.sys.executable
    assert not external.ok and "outside the workspace" in (external.error or "")


@pytest.mark.asyncio
async def test_command_scrubs_ambient_credentials_from_child_environment(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.create_run(RunRecord(id="run-1", task="Run", workspace=str(workspace.root)))
    monkeypatch.setenv("TRACEFORGE_TEST_API_KEY", "credential-probe")
    monkeypatch.setenv("TRACEFORGE_TEST_PASSPHRASE", "passphrase-probe")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent-probe.sock")
    monkeypatch.setenv("TRACEFORGE_TEST_PLAIN", "visible")
    registry = ToolRegistry(workspace, settings)

    result = await registry.execute(
        "run-1",
        ToolCall(
            id="environment",
            name="run_command",
            arguments={
                "argv": [
                    "python",
                    "-c",
                    (
                        "import os; print('|'.join(os.getenv(name, 'absent') for name in "
                        "['TRACEFORGE_TEST_API_KEY', 'TRACEFORGE_TEST_PASSPHRASE', "
                        "'SSH_AUTH_SOCK', 'TRACEFORGE_TEST_PLAIN']))"
                    ),
                ]
            },
        ),
    )

    assert result.ok
    assert result.output.strip() == "absent|absent|absent|visible"


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


@pytest.mark.asyncio
async def test_read_list_search_and_error_results(
    settings: Settings, workspace: Workspace, storage: Storage, monkeypatch
) -> None:
    storage.create_run(RunRecord(id="run-1", task="Inspect", workspace=str(workspace.root)))
    registry = ToolRegistry(workspace, settings)
    (workspace.root / "notes.txt").write_text("alpha\nbeta\n")
    (workspace.root / ".env").write_text("SECRET=value\n")
    (workspace.root / ".env.example").write_text("SECRET=\n")
    (workspace.root / "binary.bin").write_bytes(b"\xff\xfe")

    listed = await registry.execute(
        "run-1", ToolCall(id="list", name="list_files", arguments={})
    )
    read = await registry.execute(
        "run-1",
        ToolCall(
            id="read",
            name="read_file",
            arguments={"path": "notes.txt", "start_line": 2, "end_line": 2},
        ),
    )
    secret = await registry.execute(
        "run-1", ToolCall(id="secret", name="read_file", arguments={"path": ".env"})
    )
    backwards = await registry.execute(
        "run-1",
        ToolCall(
            id="backwards",
            name="read_file",
            arguments={"path": "notes.txt", "start_line": 2, "end_line": 1},
        ),
    )
    monkeypatch.setattr(tools_module.shutil, "which", lambda _name: None)
    found = await registry.execute(
        "run-1",
        ToolCall(
            id="search", name="search_text", arguments={"query": "beta", "path": "."}
        ),
    )

    assert listed.ok and ".env.example" in listed.output and "SECRET=value" not in listed.output
    assert "2 | beta" in read.output
    assert not secret.ok and "Secret-bearing" in (secret.error or "")
    assert not backwards.ok and "must not be before" in (backwards.error or "")
    assert found.ok and "notes.txt:2:beta" in found.output


@pytest.mark.asyncio
async def test_command_failure_truncation_callback_and_validation(
    settings: Settings, workspace: Workspace, storage: Storage
) -> None:
    storage.create_run(RunRecord(id="run-1", task="Run", workspace=str(workspace.root)))
    limited = replace(settings, stored_output_limit=24, model_output_limit=16)
    registry = ToolRegistry(workspace, limited)
    streamed: list[str] = []

    async def capture(chunk: str) -> None:
        streamed.append(chunk)

    result = await registry.execute(
        "run-1",
        ToolCall(
            id="large",
            name="run_command",
            arguments={"argv": ["python3", "-c", "print('x' * 100); raise SystemExit(3)"]},
        ),
        output_callback=capture,
    )
    missing = await registry.execute(
        "run-1",
        ToolCall(id="missing", name="run_command", arguments={"argv": ["not-a-program"]}),
    )
    bad_cwd_file = workspace.root / "file.txt"
    bad_cwd_file.write_text("file")
    bad_cwd = await registry.execute(
        "run-1",
        ToolCall(
            id="cwd",
            name="run_command",
            arguments={"argv": ["python3", "-V"], "cwd": "file.txt"},
        ),
    )
    too_long = await registry.execute(
        "run-1",
        ToolCall(
            id="long",
            name="run_command",
            arguments={"argv": ["python3", "-V"], "timeout_seconds": 601},
        ),
    )
    unknown = await registry.execute(
        "run-1", ToolCall(id="unknown", name="unknown", arguments={})
    )

    assert not result.ok and result.metadata["exit_code"] == 3
    assert result.metadata["truncated"] is True and "truncated" in result.output
    assert streamed and "x" in "".join(streamed)
    assert not missing.ok and "Executable not found" in (missing.error or "")
    assert not bad_cwd.ok and "not a directory" in (bad_cwd.error or "")
    assert not too_long.ok and "exceeds" in (too_long.error or "")
    assert not unknown.ok and "Unknown" in (unknown.error or "")
    await registry.cancel("run-1")


@pytest.mark.asyncio
async def test_patch_can_delete_and_reject_rename(
    settings: Settings, workspace: Workspace, storage: Storage
) -> None:
    storage.create_run(RunRecord(id="run-1", task="Patch", workspace=str(workspace.root)))
    registry = ToolRegistry(workspace, settings)
    target = workspace.root / "old.txt"
    target.write_text("old\n")

    deleted = await registry.execute(
        "run-1",
        ToolCall(
            id="delete",
            name="apply_patch",
            arguments={
                "patch": "--- a/old.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n"
            },
        ),
    )
    assert deleted.ok and not target.exists()

    source = workspace.root / "source.txt"
    source.write_text("value\n")
    renamed = await registry.execute(
        "run-1",
        ToolCall(
            id="rename",
            name="apply_patch",
            arguments={
                "patch": (
                    "--- a/source.txt\n+++ b/renamed.txt\n@@ -1 +1 @@\n-value\n+changed\n"
                )
            },
        ),
    )
    assert not renamed.ok and "renames" in (renamed.error or "")


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

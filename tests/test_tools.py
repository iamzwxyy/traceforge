from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import traceforge.tools as tools_module
from traceforge.config import Settings
from traceforge.models import (
    AcceptanceCheck,
    ApprovalMode,
    PlanStep,
    RunRecord,
    TaskPlan,
    ToolCall,
    WorkspaceInstructionSnapshot,
)
from traceforge.sandbox import SandboxStatus
from traceforge.storage import Storage
from traceforge.tools import PermissionDecision, ToolRegistry
from traceforge.workspace import Workspace


def _plan(command: list[str]) -> TaskPlan:
    return TaskPlan(
        summary="Make the tests pass",
        steps=[PlanStep(id="fix", title="Fix")],
        acceptance_checks=[AcceptanceCheck(id="test", label="Tests", command=command)],
    )


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


def test_approval_modes_are_orthogonal_to_invariant_permission_policy(
    settings: Settings, workspace: Workspace
) -> None:
    registry = ToolRegistry(workspace, settings)
    registry.sandbox.status = SandboxStatus(
        backend="seatbelt", enforced=True, detail="test sandbox"
    )
    plan = _plan(["uv", "run", "pytest"]).model_copy(
        update={"impacted_files": ["src/allowed.py"]}
    )
    planned_write = ToolCall(
        id="write",
        name="create_file",
        arguments={"path": "src/allowed.py", "content": "value = 1\n"},
    )
    drift = ToolCall(
        id="drift",
        name="create_file",
        arguments={"path": "src/drift.py", "content": "value = 2\n"},
    )
    accepted_check = ToolCall(
        id="check",
        name="run_command",
        arguments={"argv": ["uv", "run", "pytest"]},
    )
    unknown_command = ToolCall(
        id="unknown",
        name="run_command",
        arguments={"argv": ["python", "app.py"]},
    )
    read_tool = ToolCall(
        id="read", name="read_file", arguments={"path": "README.md"}
    )
    destructive = ToolCall(
        id="deny",
        name="run_command",
        arguments={"argv": ["rm", "-rf", "build"]},
    )

    for call in (planned_write, drift, accepted_check, unknown_command):
        manual = registry.resolve_permission(call, plan, ApprovalMode.MANUAL)
        assert manual.decision is PermissionDecision.ASK
        assert manual.authorization == "user"
        assert manual.sandbox_bypass_on_allow is False
    assert (
        registry.resolve_permission(read_tool, plan, ApprovalMode.MANUAL).decision
        is PermissionDecision.ALLOW
    )

    assert registry.resolve_permission(
        planned_write, plan, ApprovalMode.AUTOMATIC
    ).decision is PermissionDecision.ALLOW
    automatic_unknown = registry.resolve_permission(
        unknown_command, plan, ApprovalMode.AUTOMATIC
    )
    assert automatic_unknown.decision is PermissionDecision.ASK
    assert automatic_unknown.sandbox_bypass_on_allow is True

    for call in (planned_write, drift, accepted_check, unknown_command):
        full = registry.resolve_permission(call, plan, ApprovalMode.FULL_ACCESS)
        assert full.decision is PermissionDecision.ALLOW
        assert full.authorization == "full_access"
        assert full.sandbox_bypass_on_allow is False
    for mode in ApprovalMode:
        assert (
            registry.resolve_permission(destructive, plan, mode).decision
            is PermissionDecision.DENY
        )


def test_full_access_unknown_command_falls_back_to_human_without_os_sandbox(
    settings: Settings, workspace: Workspace
) -> None:
    registry = ToolRegistry(workspace, settings)
    registry.sandbox.status = SandboxStatus(
        backend="none", enforced=False, detail="policy only"
    )
    unknown = ToolCall(
        id="unknown",
        name="run_command",
        arguments={"argv": ["python", "app.py"]},
    )

    resolution = registry.resolve_permission(unknown, None, ApprovalMode.FULL_ACCESS)

    assert resolution.decision is PermissionDecision.ASK
    assert resolution.authorization == "user"
    assert resolution.sandbox_bypass_on_allow is True
    assert "no OS sandbox" in resolution.reason


def test_permission_policy_allows_sandboxed_variants_of_an_approved_check(
    settings: Settings, workspace: Workspace
) -> None:
    registry = ToolRegistry(workspace, settings)
    plan = _plan(["python", "-m", "pytest", "-q"])
    focused = ToolCall(
        id="focused",
        name="run_command",
        arguments={
            "argv": [
                "python3",
                "-m",
                "pytest",
                "-q",
                "tests/test_duration_parser.py::test_booleans_are_not_durations",
                "-v",
            ]
        },
    )
    different_launcher = ToolCall(
        id="uv",
        name="run_command",
        arguments={"argv": ["uv", "run", "pytest", "-q"]},
    )
    arbitrary_python = ToolCall(
        id="python-code",
        name="run_command",
        arguments={"argv": ["python", "-c", "print('custom')"]},
    )
    interactive_pytest = ToolCall(
        id="pdb",
        name="run_command",
        arguments={"argv": ["python", "-m", "pytest", "--pdb"]},
    )
    writing_pytest = ToolCall(
        id="junit",
        name="run_command",
        arguments={"argv": ["python", "-m", "pytest", "--junitxml=report.xml"]},
    )

    assessment = registry.assess(focused, plan)
    assert assessment.decision is PermissionDecision.ALLOW
    assert "variant" in assessment.reason
    assert registry.assess(different_launcher, plan).decision is PermissionDecision.ASK
    assert registry.assess(arbitrary_python, plan).decision is PermissionDecision.ASK
    assert registry.assess(interactive_pytest, plan).decision is PermissionDecision.ASK
    assert registry.assess(writing_pytest, plan).decision is PermissionDecision.ASK


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
async def test_execute_rechecks_hard_command_denials_even_if_policy_is_bypassed(
    settings: Settings, workspace: Workspace
) -> None:
    registry = ToolRegistry(workspace, settings)

    for argv in (["sudo", "true"], ["rm", "-rf", "build"], ["cat", "../../secret"]):
        result = await registry.execute(
            "run",
            ToolCall(id="hard-denial", name="run_command", arguments={"argv": argv}),
            sandbox_bypass=True,
        )
        assert result.ok is False
        assert result.error


@pytest.mark.asyncio
async def test_command_prefers_traceforge_runtime_and_rejects_external_paths(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="run-1", task="Run", workspace=str(workspace.root)),
    )
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

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
    assert runtime.metadata["sandbox"]["status"] in {"enforced", "policy_only"}
    assert not external.ok and "outside the workspace" in (external.error or "")


@pytest.mark.asyncio
async def test_command_scrubs_ambient_credentials_from_child_environment(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="run-1", task="Run", workspace=str(workspace.root)),
    )
    monkeypatch.setenv("TRACEFORGE_TEST_API_KEY", "credential-probe")
    monkeypatch.setenv("TRACEFORGE_TEST_PASSPHRASE", "passphrase-probe")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent-probe.sock")
    monkeypatch.setenv("TRACEFORGE_TEST_PLAIN", "visible")

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
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="run-1", task="Edit", workspace=str(workspace.root)),
    )

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
    assert create.metadata["changed_files"] == ["src/example.py"]
    assert patch.metadata["changed_files"] == ["src/example.py"]
    assert "changed_files" not in command.metadata
    assert (workspace.root / "src/example.py").read_text() == "value = 2\n"
    assert "verified" in command.output
    workspace.rollback("run-1")
    assert not (workspace.root / "src/example.py").exists()


@pytest.mark.asyncio
async def test_native_mutations_never_snapshot_or_write_credentials(
    settings: Settings, workspace: Workspace, storage: Storage
) -> None:
    configured = "owner-only-key-123456"
    protected_settings = replace(settings, api_key=configured)
    registry = _persisted_registry(
        storage,
        workspace,
        protected_settings,
        RunRecord(id="run-1", task="Edit safely", workspace=str(workspace.root)),
    )
    existing = workspace.root / "existing.txt"
    existing.write_text(f"first\n{configured}\n")
    safe_existing = workspace.root / "safe-existing.txt"
    safe_existing.write_text("safe\n")

    patch = await registry.execute(
        "run-1",
        ToolCall(
            id="patch",
            name="apply_patch",
            arguments={
                "patch": (
                    "--- a/existing.txt\n+++ b/existing.txt\n"
                    "@@ -1 +1 @@\n-first\n+changed\n"
                )
            },
        ),
    )
    create = await registry.execute(
        "run-1",
        ToolCall(
            id="create",
            name="create_file",
            arguments={"path": "new.txt", "content": configured},
        ),
    )
    insert = await registry.execute(
        "run-1",
        ToolCall(
            id="insert",
            name="apply_patch",
            arguments={
                "patch": (
                    "--- a/safe-existing.txt\n+++ b/safe-existing.txt\n"
                    "@@ -1 +1,2 @@\n safe\n"
                    f"+{configured}\n"
                )
            },
        ),
    )

    assert not patch.ok and "credential-like data" in (patch.error or "")
    assert not create.ok and "credential-like data" in (create.error or "")
    assert not insert.ok and "credential-like data" in (insert.error or "")
    assert existing.read_text() == f"first\n{configured}\n"
    assert safe_existing.read_text() == "safe\n"
    assert not (workspace.root / "new.txt").exists()
    assert storage.list_snapshots("run-1") == []
    for database_file in settings.data_dir.glob("test.db*"):
        assert configured.encode() not in database_file.read_bytes()


@pytest.mark.asyncio
async def test_mutation_metadata_reports_only_files_that_actually_changed(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="run-1", task="Patch", workspace=str(workspace.root)),
    )
    first = workspace.root / "first.txt"
    second = workspace.root / "second.txt"
    first.write_text("before one\n")
    second.write_text("before two\n")
    original_write_text = Path.write_text

    def fail_second_write(path: Path, content: str, **kwargs: object) -> int:
        if path.resolve() == second.resolve():
            raise OSError("simulated second-file write failure")
        return original_write_text(path, content, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_second_write)
    result = await registry.execute(
        "run-1",
        ToolCall(
            id="partial",
            name="apply_patch",
            arguments={
                "patch": (
                    "--- a/first.txt\n+++ b/first.txt\n"
                    "@@ -1 +1 @@\n-before one\n+after one\n"
                    "--- a/second.txt\n+++ b/second.txt\n"
                    "@@ -1 +1 @@\n-before two\n+after two\n"
                )
            },
        ),
    )

    assert not result.ok
    assert result.metadata["changed_files"] == ["first.txt"]
    assert first.read_text() == "after one\n"
    assert second.read_text() == "before two\n"


@pytest.mark.asyncio
async def test_mutation_metadata_canonicalizes_aliases_and_ignores_no_ops(
    settings: Settings, workspace: Workspace, storage: Storage
) -> None:
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="run-1", task="Patch", workspace=str(workspace.root)),
    )
    target = workspace.root / "src" / "example.py"
    target.parent.mkdir()
    target.write_text("value = 1\n")
    call = ToolCall(
        id="noop",
        name="apply_patch",
        arguments={
            "patch": (
                "--- a/src/../src/example.py\n"
                "+++ b/src/../src/example.py\n"
                "@@ -1 +1 @@\n"
                "-value = 1\n"
                "+value = 1\n"
            )
        },
    )
    plan = _plan(["uv", "run", "pytest"]).model_copy(
        update={"impacted_files": ["src/example.py"]}
    )

    assert registry.assess(call, plan).decision is PermissionDecision.ALLOW
    result = await registry.execute("run-1", call)

    assert result.ok
    assert result.metadata["changed_files"] == []
    assert target.read_text() == "value = 1\n"


@pytest.mark.asyncio
async def test_command_timeout(
    settings: Settings, workspace: Workspace, storage: Storage
) -> None:
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="run-1", task="Wait", workspace=str(workspace.root)),
    )
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
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="run-1", task="Inspect", workspace=str(workspace.root)),
    )
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
    limited = replace(settings, stored_output_limit=24, model_output_limit=16)
    registry = _persisted_registry(
        storage,
        workspace,
        limited,
        RunRecord(id="run-1", task="Run", workspace=str(workspace.root)),
    )
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
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="run-1", task="Patch", workspace=str(workspace.root)),
    )
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

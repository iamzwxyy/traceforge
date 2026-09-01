from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import threading
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


def _create_uv_isolation_probe(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace_root = tmp_path / "self-host-workspace"
    workspace_root.mkdir()
    private_prefix = Path(tools_module.sys.prefix).resolve()
    (workspace_root / ".venv").symlink_to(private_prefix, target_is_directory=True)
    (workspace_root / "pyproject.toml").write_text(
        "[project]\nname = 'isolated-probe'\nversion = '0.0.0'\n"
        "requires-python = '>=3.12,<3.13'\ndependencies = []\n",
        encoding="utf-8",
    )
    isolated_environment = workspace_root / ".traceforge-uv-venv"
    uv = shutil.which("uv")
    assert uv is not None
    base_python = Path(
        getattr(tools_module.sys, "_base_executable", tools_module.sys.executable)
    ).resolve()
    subprocess.run(
        [
            uv,
            "venv",
            "--python",
            str(base_python),
            "--no-python-downloads",
            str(isolated_environment),
        ],
        cwd=workspace_root,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return workspace_root, isolated_environment.resolve(), private_prefix


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


@pytest.mark.parametrize(
    "call",
    [
        ToolCall(
            id="create-with-empty-plan",
            name="create_file",
            arguments={"path": "src/unplanned.py", "content": "value = 1\n"},
        ),
        ToolCall(
            id="patch-with-empty-plan",
            name="apply_patch",
            arguments={
                "patch": (
                    "*** Begin Patch\n"
                    "*** Add File: src/unplanned.py\n"
                    "+value = 1\n"
                    "*** End Patch"
                )
            },
        ),
    ],
    ids=["create-file", "apply-patch"],
)
def test_permission_policy_denies_mutation_when_plan_has_no_impacted_files(
    settings: Settings,
    workspace: Workspace,
    call: ToolCall,
) -> None:
    registry = ToolRegistry(workspace, settings)
    plan = _plan(["uv", "run", "pytest"])

    assessment = registry.assess(call, plan)

    assert assessment.decision is PermissionDecision.DENY
    assert assessment.risk == "dangerous"
    assert "declares no impacted files" in assessment.reason
    for mode in ApprovalMode:
        resolution = registry.resolve_permission(call, plan, mode)
        assert resolution.decision is PermissionDecision.DENY
        assert resolution.policy_decision is PermissionDecision.DENY
        assert resolution.authorization == "policy"


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
async def test_command_does_not_borrow_traceforge_runtime_and_rejects_external_paths(
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
            arguments={
                "argv": [
                    "python3",
                    "-c",
                    (
                        "import sys; print(sys.executable); "
                        "print(f'{sys.version_info.major}.{sys.version_info.minor}')"
                    ),
                ]
            },
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
    executable, version = runtime.output.splitlines()
    base_executable = Path(
        getattr(tools_module.sys, "_base_executable", tools_module.sys.executable)
    )
    resolved_executable, resolved_base = await asyncio.gather(
        asyncio.to_thread(Path(executable).resolve),
        asyncio.to_thread(base_executable.resolve),
    )
    assert resolved_executable == resolved_base
    assert version == "3.12"
    if tools_module.sys.prefix != tools_module.sys.base_prefix:
        executable_parent, private_parent = await asyncio.gather(
            asyncio.to_thread(Path(executable).parent.resolve),
            asyncio.to_thread(Path(tools_module.sys.executable).parent.resolve),
        )
        assert executable_parent != private_parent
    assert runtime.metadata["sandbox"]["status"] in {"enforced", "policy_only"}
    assert not external.ok and "outside the workspace" in (external.error or "")


@pytest.mark.skipif(
    tools_module.sys.prefix == tools_module.sys.base_prefix,
    reason="The test process is not running from an isolated Agent environment",
)
def test_command_environment_filters_private_runtime_even_when_workspace_contains_it(
    tmp_path: Path,
) -> None:
    private_runtime = Path(tools_module.sys.executable).parent.resolve()
    workspace = Path(tools_module.sys.prefix).parent

    environment = tools_module._command_environment(
        workspace,
        home=tmp_path / "home",
        temp=tmp_path / "tmp",
        cache=tmp_path / "cache",
    )

    path_entries = [Path(entry).resolve() for entry in environment["PATH"].split(os.pathsep)]
    assert private_runtime not in path_entries
    assert tools_module._base_python_runtime_directory() in path_entries
    assert Path(environment["UV_PROJECT_ENVIRONMENT"]) == (
        workspace / ".traceforge-uv-venv"
    ).resolve()
    assert environment["PIP_REQUIRE_VIRTUALENV"] == "1"


@pytest.mark.asyncio
@pytest.mark.skipif(
    tools_module.sys.prefix == tools_module.sys.base_prefix or shutil.which("uv") is None,
    reason="The test needs uv and an isolated Agent environment",
)
async def test_uv_run_cannot_rediscover_the_agent_private_environment(
    tmp_path: Path,
    settings: Settings,
    storage: Storage,
) -> None:
    workspace_root, isolated_environment, private_prefix = await asyncio.to_thread(
        _create_uv_isolation_probe, tmp_path
    )
    workspace = Workspace(workspace_root, storage)
    isolated_settings = replace(settings, workspace=workspace_root)
    registry = _persisted_registry(
        storage,
        workspace,
        isolated_settings,
        RunRecord(id="uv-isolation", task="Run uv", workspace=str(workspace_root)),
    )

    result = await registry.execute(
        "uv-isolation",
        ToolCall(
            id="uv-run",
            name="run_command",
            arguments={
                "argv": [
                    "uv",
                    "run",
                    "--no-sync",
                    "python",
                    "-c",
                    (
                        "import os, pathlib, sys; "
                        "print(pathlib.Path(sys.prefix).resolve()); "
                        "print(pathlib.Path(os.environ['UV_PROJECT_ENVIRONMENT']).resolve()); "
                        "print(os.environ['PIP_REQUIRE_VIRTUALENV'])"
                    ),
                ]
            },
        ),
    )

    assert result.ok, result.error or result.output
    prefix, selected_environment, pip_guard = result.output.splitlines()
    assert Path(prefix) == isolated_environment
    assert Path(selected_environment) == isolated_environment
    assert Path(prefix) != private_prefix
    assert pip_guard == "1"


@pytest.mark.asyncio
@pytest.mark.skipif(
    tools_module.sys.prefix == tools_module.sys.base_prefix,
    reason="The test process is not running from an isolated Agent environment",
)
async def test_command_rejects_an_explicit_agent_private_executable(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
) -> None:
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="run-1", task="Run", workspace=str(workspace.root)),
    )

    result = await registry.execute(
        "run-1",
        ToolCall(
            id="private-runtime",
            name="run_command",
            arguments={
                "argv": [tools_module.sys.executable, "-c", "print('unreachable')"]
            },
        ),
    )

    assert result.ok is False
    assert "TraceForge's private runtime" in (result.error or "")


@pytest.mark.asyncio
@pytest.mark.skipif(
    tools_module.sys.prefix == tools_module.sys.base_prefix,
    reason="The test process is not running from an isolated Agent environment",
)
async def test_command_rejects_workspace_symlink_to_agent_private_pytest(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
) -> None:
    private_pytest = Path(tools_module.sys.executable).parent / "pytest"
    if not private_pytest.is_file():
        pytest.skip("The Agent environment does not contain a pytest launcher")
    workspace_bin = workspace.root / ".venv" / "bin"
    workspace_bin.mkdir(parents=True)
    (workspace_bin / "pytest").symlink_to(private_pytest)
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="symlinked-private-runtime", task="Run", workspace=str(workspace.root)),
    )

    result = await registry.execute(
        "symlinked-private-runtime",
        ToolCall(
            id="symlinked-pytest",
            name="run_command",
            arguments={"argv": ["pytest", "--version"]},
        ),
    )

    assert result.ok is False
    assert "TraceForge's private runtime" in (result.error or "")


@pytest.mark.asyncio
@pytest.mark.skipif(
    tools_module.sys.prefix == tools_module.sys.base_prefix,
    reason="The test process is not running from an isolated Agent environment",
)
async def test_command_rejects_copied_launcher_with_agent_private_shebang(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
) -> None:
    private_pytest = Path(tools_module.sys.executable).parent / "pytest"
    if not private_pytest.is_file():
        pytest.skip("The Agent environment does not contain a pytest launcher")
    workspace_bin = workspace.root / ".venv" / "bin"
    workspace_bin.mkdir(parents=True)
    copied_launcher = workspace_bin / "copied-pytest"
    shutil.copy2(private_pytest, copied_launcher)
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="copied-private-runtime", task="Run", workspace=str(workspace.root)),
    )

    result = await registry.execute(
        "copied-private-runtime",
        ToolCall(
            id="copied-pytest",
            name="run_command",
            arguments={"argv": ["copied-pytest", "--version"]},
        ),
    )

    assert result.ok is False
    assert "TraceForge's private runtime" in (result.error or "")


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
    monkeypatch.setenv("VIRTUAL_ENV", "/outside/traceforge-runtime")
    monkeypatch.setenv("VIRTUAL_ENV_PROMPT", "traceforge")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/outside/shared-environment")
    monkeypatch.setenv("PYTHONHOME", "/outside/python-home")
    monkeypatch.setenv("PYTHONPATH", "/outside/python-path")

    result = await registry.execute(
        "run-1",
        ToolCall(
            id="environment",
            name="run_command",
            arguments={
                "argv": [
                    "python3",
                    "-c",
                    (
                        "import os; print('|'.join(os.getenv(name, 'absent') for name in "
                        "['TRACEFORGE_TEST_API_KEY', 'TRACEFORGE_TEST_PASSPHRASE', "
                        "'SSH_AUTH_SOCK', 'TRACEFORGE_TEST_PLAIN', 'VIRTUAL_ENV', "
                        "'VIRTUAL_ENV_PROMPT', 'UV_PROJECT_ENVIRONMENT', 'PYTHONHOME', "
                        "'PYTHONPATH']))"
                    ),
                ]
            },
        ),
    )

    assert result.ok
    assert result.output.strip() == (
        "absent|absent|absent|visible|absent|absent|absent|absent|absent"
    )


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
async def test_unscoped_search_rejects_explicit_environment_paths_and_aliases(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="env-search-boundary", task="Inspect", workspace=str(workspace.root)),
    )
    sensitive_content = "PASSWORD=plain-environment-secret\n"
    (workspace.root / ".env").write_text(sensitive_content)
    (workspace.root / ".ENV").write_text(sensitive_content)
    (workspace.root / ".env.prod").write_text(sensitive_content)
    protected_directory = workspace.root / "nested" / ".EnV.prod"
    protected_directory.mkdir(parents=True)
    (protected_directory / "settings.txt").write_text(sensitive_content)
    (workspace.root / "environment-alias").symlink_to(workspace.root / ".env")
    (workspace.root / ".env.example").write_text("EXAMPLE_VALUE=placeholder\n")

    async def reject_subprocess(*_args, **_kwargs):
        raise AssertionError("protected paths must be rejected before rg starts")

    monkeypatch.setattr(tools_module.shutil, "which", lambda _name: "/usr/bin/rg")
    monkeypatch.setattr(
        tools_module.asyncio,
        "create_subprocess_exec",
        reject_subprocess,
    )
    protected_paths = [
        ".env",
        ".ENV",
        ".env.prod",
        "nested/.EnV.prod/settings.txt",
        "environment-alias",
    ]
    for index, path in enumerate(protected_paths):
        result = await registry.execute(
            "env-search-boundary",
            ToolCall(
                id=f"protected-{index}",
                name="search_text",
                arguments={"query": "PASSWORD", "path": path},
            ),
        )
        assert not result.ok
        if path != "environment-alias":
            assert "Secret-bearing environment paths" in (result.error or "")
        assert "plain-environment-secret" not in f"{result.output}\n{result.error}"

    monkeypatch.setattr(tools_module.shutil, "which", lambda _name: None)
    example = await registry.execute(
        "env-search-boundary",
        ToolCall(
            id="allowed-example",
            name="search_text",
            arguments={"query": "EXAMPLE_VALUE", "path": ".env.example"},
        ),
    )
    assert example.ok, example.error
    assert ".env.example:1:EXAMPLE_VALUE=placeholder" in example.output


@pytest.mark.asyncio
async def test_unscoped_listing_streams_a_bounded_number_of_directory_entries(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(20):
        (workspace.root / f"entry-{index:02}.txt").write_text("bounded\n")
    monkeypatch.setattr(tools_module, "SCOPED_LIST_MAX_ENTRIES", 5)
    monkeypatch.setattr(tools_module, "SCOPED_LIST_MAX_ROWS", 100)

    def reject_listdir(_path: object) -> list[str]:
        raise AssertionError("bounded workspace listing must stream with scandir")

    monkeypatch.setattr(tools_module.os, "listdir", reject_listdir)
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="workspace-list-cap", task="Inspect", workspace=str(workspace.root)),
    )

    result = await registry.execute(
        "workspace-list-cap",
        ToolCall(id="list", name="list_files", arguments={}),
    )

    assert result.ok, result.error
    assert result.output.endswith("... output truncated")
    assert len([line for line in result.output.splitlines() if line.startswith("entry-")]) <= 5
    assert result.metadata == {}


@pytest.mark.asyncio
async def test_unscoped_file_reads_and_search_outputs_obey_hard_resource_caps(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (workspace.root / "oversized.txt").write_text("line\n" * 100)
    (workspace.root / "long-line.txt").write_text("x" * 40 + "\n")
    (workspace.root / "matches.txt").write_text(
        "\n".join(f"needle value {index:03} " + "y" * 20 for index in range(20)) + "\n"
    )
    monkeypatch.setattr(tools_module, "SCOPED_READ_MAX_FILE_BYTES", 64)
    monkeypatch.setattr(tools_module, "SCOPED_READ_MAX_LINE_BYTES", 32)
    limited = replace(settings, model_output_limit=48, stored_output_limit=40)
    registry = _persisted_registry(
        storage,
        workspace,
        limited,
        RunRecord(id="workspace-read-caps", task="Inspect", workspace=str(workspace.root)),
    )

    oversized = await registry.execute(
        "workspace-read-caps",
        ToolCall(
            id="oversized",
            name="read_file",
            arguments={"path": "oversized.txt", "start_line": 20, "end_line": 20},
        ),
    )
    long_line = await registry.execute(
        "workspace-read-caps",
        ToolCall(id="long-line", name="read_file", arguments={"path": "long-line.txt"}),
    )
    searched = await registry.execute(
        "workspace-read-caps",
        ToolCall(id="search", name="search_text", arguments={"query": "needle"}),
    )

    assert not oversized.ok and "read limit" in (oversized.error or "")
    assert not long_line.ok and "requested line exceeds" in (long_line.error or "")
    assert searched.ok, searched.error
    assert searched.output.endswith("... output truncated")
    assert len(searched.output) <= limited.model_output_limit
    assert len(searched.output.encode("utf-8")) <= limited.stored_output_limit
    assert searched.metadata == {}


@pytest.mark.asyncio
async def test_unscoped_reads_run_off_loop_and_reject_rename_to_external_symlink(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raced = workspace.root / "race.txt"
    raced.write_text("original workspace content\n")
    outside = workspace.root.parent / "outside-secret.txt"
    sensitive_content = "external secret from rename race"
    outside.write_text(sensitive_content)
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="workspace-race", task="Inspect", workspace=str(workspace.root)),
    )
    event_loop_thread = threading.get_ident()
    worker_threads: list[int] = []
    real_execute = registry._execute_workspace_read
    real_open = registry._open_scoped_node
    swapped = False

    def record_execute(call: ToolCall):
        worker_threads.append(threading.get_ident())
        return real_execute(call)

    def swap_before_open(*args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            raced.rename(workspace.root / "race-original.txt")
            raced.symlink_to(outside)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(registry, "_execute_workspace_read", record_execute)
    monkeypatch.setattr(registry, "_open_scoped_node", swap_before_open)
    result = await registry.execute(
        "workspace-race",
        ToolCall(id="read", name="read_file", arguments={"path": "race.txt"}),
    )

    assert swapped is True
    assert not result.ok
    assert sensitive_content not in f"{result.output}\n{result.error}"
    assert worker_threads and worker_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_project_scope_is_the_virtual_root_for_read_tools_and_can_be_cleared(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha = workspace.root / "alpha"
    beta = workspace.root / "beta"
    (alpha / "src").mkdir(parents=True)
    (alpha / "alpha").mkdir()
    beta.mkdir()
    (alpha / "README.md").write_text("alpha project\nneedle-alpha\n")
    (alpha / "alpha" / "README.md").write_text(
        "nested alpha directory\ninner-only\n"
    )
    (alpha / "src" / "main.py").write_text("print('alpha')\n")
    (beta / "README.md").write_text("beta private payload\n")
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="scoped-read", task="Inspect alpha", workspace=str(workspace.root)),
    )
    scope_identity = registry.bind_project_scope("scoped-read", "alpha")

    listed = await registry.execute(
        "scoped-read", ToolCall(id="list", name="list_files", arguments={})
    )
    read = await registry.execute(
        "scoped-read",
        ToolCall(id="read", name="read_file", arguments={"path": "README.md"}),
    )
    explicit = await registry.execute(
        "scoped-read",
        ToolCall(
            id="explicit",
            name="read_file",
            arguments={"path": "alpha/README.md"},
        ),
    )
    monkeypatch.setattr(tools_module.shutil, "which", lambda _name: None)
    searched = await registry.execute(
        "scoped-read",
        ToolCall(
            id="search",
            name="search_text",
            arguments={"query": "needle-alpha"},
        ),
    )

    assert listed.ok
    listed_paths = set(listed.output.splitlines())
    assert {"README.md", "src/", "alpha/", "alpha/README.md"} <= listed_paths
    assert "beta" not in listed.output
    assert listed.metadata == {
        "project_scope": "alpha",
        "project_scope_identity": scope_identity,
        "requested_path": ".",
        "effective_path": "alpha",
    }
    assert read.ok and "needle-alpha" in read.output
    assert read.metadata["effective_path"] == "alpha/README.md"
    assert explicit.ok and "inner-only" in explicit.output
    assert "needle-alpha" not in explicit.output
    assert explicit.metadata["effective_path"] == "alpha/alpha/README.md"
    assert searched.ok and "README.md:2:needle-alpha" in searched.output
    assert "alpha/README.md:2:needle-alpha" not in searched.output
    assert searched.metadata["effective_path"] == "alpha"
    assert "beta private payload" not in "".join(
        result.output for result in (listed, read, explicit, searched)
    )

    registry.clear_project_scope("scoped-read")
    unscoped = await registry.execute(
        "scoped-read", ToolCall(id="unscoped", name="list_files", arguments={})
    )
    assert unscoped.ok and "alpha/" in unscoped.output and "beta/" in unscoped.output
    assert unscoped.metadata == {}


@pytest.mark.asyncio
async def test_bound_project_scope_is_a_virtual_root_for_native_mutations(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
) -> None:
    alpha = workspace.root / "alpha"
    beta = workspace.root / "beta"
    (alpha / "src").mkdir(parents=True)
    beta.mkdir()
    (alpha / "src" / "existing.py").write_text("value = 'alpha'\n")
    beta_target = beta / "secret.py"
    beta_target.write_text("value = 'beta'\n")
    run_id = "scoped-mutations"
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id=run_id, task="Edit alpha", workspace=str(workspace.root)),
    )
    original_identity = registry.bind_project_scope(run_id, "alpha")

    created = await registry.execute(
        run_id,
        ToolCall(
            id="create",
            name="create_file",
            arguments={"path": "src/generated.py", "content": "generated = True\n"},
        ),
    )
    patched = await registry.execute(
        run_id,
        ToolCall(
            id="patch",
            name="apply_patch",
            arguments={
                "patch": (
                    "--- a/src/existing.py\n"
                    "+++ b/src/existing.py\n"
                    "@@ -1 +1 @@\n"
                    "-value = 'alpha'\n"
                    "+value = 'updated'\n"
                )
            },
        ),
    )
    create_escape = await registry.execute(
        run_id,
        ToolCall(
            id="create-escape",
            name="create_file",
            arguments={"path": "../beta/new.py", "content": "escaped = True\n"},
        ),
    )
    patch_escape = await registry.execute(
        run_id,
        ToolCall(
            id="patch-escape",
            name="apply_patch",
            arguments={
                "patch": (
                    "--- a/../beta/secret.py\n"
                    "+++ b/../beta/secret.py\n"
                    "@@ -1 +1 @@\n"
                    "-value = 'beta'\n"
                    "+value = 'escaped'\n"
                )
            },
        ),
    )

    assert created.ok and patched.ok
    assert created.metadata["changed_files"] == ["alpha/src/generated.py"]
    assert patched.metadata["changed_files"] == ["alpha/src/existing.py"]
    assert created.metadata["project_scope"] == "alpha"
    assert created.metadata["project_scope_identity"] == original_identity
    assert (alpha / "src" / "generated.py").read_text() == "generated = True\n"
    assert (alpha / "src" / "existing.py").read_text() == "value = 'updated'\n"
    assert not create_escape.ok and not patch_escape.ok
    assert "selected project scope" in (create_escape.error or "")
    assert "selected project scope" in (patch_escape.error or "")
    assert not (beta / "new.py").exists()
    assert beta_target.read_text() == "value = 'beta'\n"


@pytest.mark.asyncio
async def test_scoped_root_create_refreshes_identity_and_keeps_scope_usable(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
) -> None:
    alpha = workspace.root / "alpha"
    alpha.mkdir()
    run_id = "scoped-generation-refresh"
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id=run_id, task="Edit alpha", workspace=str(workspace.root)),
    )
    original_identity = registry.bind_project_scope(run_id, "alpha")

    created = await registry.execute(
        run_id,
        ToolCall(
            id="root-create",
            name="create_file",
            arguments={"path": "root.txt", "content": "root-level\n"},
        ),
    )
    read_back = await registry.execute(
        run_id,
        ToolCall(id="read-back", name="read_file", arguments={"path": "root.txt"}),
    )
    registry.sandbox.status = SandboxStatus("seatbelt", True, "test backend")
    registry.sandbox._program = "/usr/bin/true"
    command = await registry.execute(
        run_id,
        ToolCall(
            id="command-after-create",
            name="run_command",
            arguments={"argv": ["python3", "-V"]},
        ),
    )

    refreshed_identity = registry.current_project_scope_identity(run_id)
    assert created.ok and read_back.ok and command.ok
    assert "root-level" in read_back.output
    assert refreshed_identity is not None and refreshed_identity != original_identity
    assert created.metadata["project_scope_identity"] == refreshed_identity
    assert read_back.metadata["project_scope_identity"] == refreshed_identity
    assert command.metadata["project_scope_identity"] == refreshed_identity


@pytest.mark.asyncio
async def test_bound_project_scope_rejects_command_cwd_and_retains_sandbox_on_approval(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
) -> None:
    alpha = workspace.root / "alpha"
    beta = workspace.root / "beta"
    alpha.mkdir()
    beta.mkdir()
    sibling_executable = beta / "tool"
    sibling_executable.write_text("#!/bin/sh\nexit 0\n")
    sibling_executable.chmod(0o755)
    linked_sibling_executable = alpha / "linked-tool"
    linked_sibling_executable.symlink_to(sibling_executable)
    run_id = "scoped-command-boundary"
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id=run_id, task="Run alpha", workspace=str(workspace.root)),
    )
    registry.bind_project_scope(run_id, "alpha")
    registry.sandbox.status = SandboxStatus("seatbelt", True, "test backend")
    registry.sandbox._program = "/usr/bin/true"

    cwd_escape = await registry.execute(
        run_id,
        ToolCall(
            id="cwd-escape",
            name="run_command",
            arguments={"argv": ["python3", "-V"], "cwd": "../beta"},
        ),
    )
    approved = await registry.execute(
        run_id,
        ToolCall(
            id="approved-command",
            name="run_command",
            arguments={"argv": ["python3", "-c", "print('not launched')"]},
        ),
        sandbox_bypass=True,
    )
    executable_escapes = [
        ToolCall(
            id="sibling-executable",
            name="run_command",
            arguments={"argv": [str(sibling_executable)]},
        ),
        ToolCall(
            id="linked-sibling-executable",
            name="run_command",
            arguments={"argv": [str(linked_sibling_executable)]},
        ),
    ]

    assert not cwd_escape.ok
    assert "selected project scope" in (cwd_escape.error or "")
    assert approved.ok
    assert approved.metadata["sandbox"]["status"] == "enforced"
    assert approved.metadata["sandbox"]["scope_enforced"] is True
    assert approved.metadata["sandbox"]["bypass_requested"] is True
    for call in executable_escapes:
        assessment = registry.assess(call, None, run_id=run_id)
        assert assessment.decision is PermissionDecision.DENY
        assert "outside the selected project root" in assessment.reason
        result = await registry.execute(run_id, call, sandbox_bypass=True)
        assert not result.ok
        assert "outside the selected project root" in (result.error or "")


@pytest.mark.asyncio
async def test_bound_mutation_fails_closed_after_project_identity_replacement(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
) -> None:
    alpha = workspace.root / "alpha"
    alpha.mkdir()
    run_id = "scoped-mutation-replacement"
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id=run_id, task="Edit alpha", workspace=str(workspace.root)),
    )
    registry.bind_project_scope(run_id, "alpha")
    alpha.rename(workspace.root / "alpha-original")
    alpha.mkdir()

    result = await registry.execute(
        run_id,
        ToolCall(
            id="replaced-create",
            name="create_file",
            arguments={"path": "blocked.txt", "content": "must not write\n"},
        ),
    )

    assert not result.ok
    assert "replaced after selection" in (result.error or "")
    assert not (alpha / "blocked.txt").exists()


def test_scoped_permission_assessment_uses_effective_project_paths(
    settings: Settings,
    workspace: Workspace,
) -> None:
    alpha = workspace.root / "alpha"
    beta = workspace.root / "beta"
    (alpha / "src").mkdir(parents=True)
    beta.mkdir()
    registry = ToolRegistry(workspace, settings)
    run_id = "scoped-assessment"
    registry.bind_project_scope(run_id, "alpha")
    registry.sandbox.status = SandboxStatus("seatbelt", True, "test backend")
    plan = _plan(["uv", "run", "pytest"]).model_copy(
        update={"impacted_files": ["src/allowed.py"]}
    )

    allowed = registry.assess(
        ToolCall(
            id="allowed",
            name="create_file",
            arguments={"path": "src/allowed.py", "content": "value = 1\n"},
        ),
        plan,
        run_id=run_id,
    )
    denied_write = registry.assess(
        ToolCall(
            id="denied-write",
            name="create_file",
            arguments={"path": "../beta/escape.py", "content": "value = 2\n"},
        ),
        plan,
        run_id=run_id,
    )
    denied_command = registry.assess(
        ToolCall(
            id="denied-command",
            name="run_command",
            arguments={"argv": ["cat", "../beta/secret.txt"]},
        ),
        None,
        run_id=run_id,
    )
    approved_but_scoped = registry.resolve_permission(
        ToolCall(
            id="unknown-command",
            name="run_command",
            arguments={"argv": ["python3", "app.py"]},
        ),
        None,
        ApprovalMode.AUTOMATIC,
        run_id=run_id,
    )

    assert allowed.decision is PermissionDecision.ALLOW
    assert denied_write.decision is PermissionDecision.DENY
    assert denied_command.decision is PermissionDecision.DENY
    assert approved_but_scoped.decision is PermissionDecision.ASK
    assert approved_but_scoped.sandbox_bypass_on_allow is False


@pytest.mark.asyncio
async def test_project_scope_rejects_parent_and_symlink_escapes_without_leaking_content(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
) -> None:
    alpha = workspace.root / "alpha"
    beta = workspace.root / "beta"
    alpha.mkdir()
    beta.mkdir()
    sensitive_content = "beta private payload must stay hidden"
    (beta / "secret.txt").write_text(sensitive_content)
    (alpha / "link").symlink_to(beta, target_is_directory=True)
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="scoped-escape", task="Inspect alpha", workspace=str(workspace.root)),
    )
    registry.bind_project_scope("scoped-escape", "alpha")

    traversal = await registry.execute(
        "scoped-escape",
        ToolCall(
            id="traversal",
            name="read_file",
            arguments={"path": "../beta/secret.txt"},
        ),
    )
    linked_list = await registry.execute(
        "scoped-escape",
        ToolCall(
            id="linked-list",
            name="list_files",
            arguments={"path": "link"},
        ),
    )
    linked_search = await registry.execute(
        "scoped-escape",
        ToolCall(
            id="linked-search",
            name="search_text",
            arguments={"query": "private payload", "path": "link"},
        ),
    )

    for result in (traversal, linked_list, linked_search):
        assert not result.ok
        assert "selected project scope" in (result.error or "")
        assert sensitive_content not in f"{result.output}\n{result.error}"


@pytest.mark.asyncio
async def test_scoped_reads_reject_casefolded_git_and_environment_aliases(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
) -> None:
    alpha = workspace.root / "alpha"
    git_directory = alpha / ".GIT"
    git_directory.mkdir(parents=True)
    (git_directory / "config").write_text("private git config\n")
    (alpha / ".ENV").write_text("SECRET=uppercase\n")
    (alpha / ".ENV.prod").write_text("SECRET=production\n")
    (alpha / ".ENV.EXAMPLE").write_text("SAFE=example\n")
    protected_directory = alpha / ".EnV.local"
    protected_directory.mkdir()
    directory_secret = "SECRET=protected-directory-payload"
    (protected_directory / "settings.txt").write_text(directory_secret + "\n")
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="scoped-casefold", task="Inspect alpha", workspace=str(workspace.root)),
    )
    registry.bind_project_scope("scoped-casefold", "alpha")

    listed = await registry.execute(
        "scoped-casefold", ToolCall(id="list", name="list_files", arguments={})
    )
    git_read = await registry.execute(
        "scoped-casefold",
        ToolCall(id="git", name="read_file", arguments={"path": ".GIT/config"}),
    )
    env_read = await registry.execute(
        "scoped-casefold",
        ToolCall(id="env", name="read_file", arguments={"path": ".ENV"}),
    )
    env_prod_read = await registry.execute(
        "scoped-casefold",
        ToolCall(id="env-prod", name="read_file", arguments={"path": ".ENV.prod"}),
    )
    example_read = await registry.execute(
        "scoped-casefold",
        ToolCall(id="example", name="read_file", arguments={"path": ".ENV.EXAMPLE"}),
    )
    git_search = await registry.execute(
        "scoped-casefold",
        ToolCall(
            id="search",
            name="search_text",
            arguments={"query": "private", "path": ".GIT"},
        ),
    )
    env_directory_read = await registry.execute(
        "scoped-casefold",
        ToolCall(
            id="env-directory-read",
            name="read_file",
            arguments={"path": ".EnV.local/settings.txt"},
        ),
    )
    env_directory_search = await registry.execute(
        "scoped-casefold",
        ToolCall(
            id="env-directory-search",
            name="search_text",
            arguments={"query": "protected-directory-payload"},
        ),
    )

    assert listed.ok
    listed_paths = set(listed.output.splitlines())
    assert ".GIT" not in listed_paths
    assert ".ENV" not in listed_paths
    assert ".ENV.prod" not in listed_paths
    assert ".EnV.local/" not in listed_paths
    assert ".ENV.EXAMPLE" in listed_paths
    for result in (git_read, env_read, env_prod_read, git_search, env_directory_read):
        assert not result.ok
        assert "private git config" not in f"{result.output}\n{result.error}"
        assert "SECRET=" not in f"{result.output}\n{result.error}"
    assert env_directory_search.ok and env_directory_search.output == "No matches"
    assert directory_secret not in env_directory_search.output
    assert example_read.ok and "SAFE=example" in example_read.output

    registry.clear_project_scope("scoped-casefold")
    unscoped_list = await registry.execute(
        "scoped-casefold", ToolCall(id="unscoped-list", name="list_files", arguments={})
    )
    unscoped_search = await registry.execute(
        "scoped-casefold",
        ToolCall(
            id="unscoped-search",
            name="search_text",
            arguments={"query": "protected-directory-payload"},
        ),
    )
    unscoped_read = await registry.execute(
        "scoped-casefold",
        ToolCall(
            id="unscoped-read",
            name="read_file",
            arguments={"path": "alpha/.EnV.local/settings.txt"},
        ),
    )
    assert unscoped_list.ok and ".EnV.local" not in unscoped_list.output
    assert unscoped_search.ok and unscoped_search.output == "No matches"
    assert not unscoped_read.ok
    assert directory_secret not in (
        f"{unscoped_list.output}\n{unscoped_search.output}\n"
        f"{unscoped_read.output}\n{unscoped_read.error}"
    )


@pytest.mark.asyncio
async def test_scoped_listing_bounds_non_rendered_entries_without_holding_child_fds(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha = workspace.root / "alpha"
    outside = workspace.root / "outside.txt"
    alpha.mkdir()
    outside.write_text("outside\n")
    for index in range(20):
        (alpha / f"link-{index:02d}").symlink_to(outside)
    monkeypatch.setattr(tools_module, "SCOPED_LIST_MAX_ENTRIES", 5)
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="scoped-list-cap", task="Inspect alpha", workspace=str(workspace.root)),
    )
    registry.bind_project_scope("scoped-list-cap", "alpha")
    real_open = tools_module.os.open
    real_dup = tools_module.os.dup
    descriptors: list[int] = []

    def record_open(path, flags, mode=0o777, *, dir_fd=None):
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        descriptors.append(descriptor)
        return descriptor

    def record_dup(descriptor: int) -> int:
        duplicate = real_dup(descriptor)
        descriptors.append(duplicate)
        return duplicate

    def reject_unbounded_listdir(_descriptor):
        raise AssertionError("scoped listing must stream bounded directory entries")

    monkeypatch.setattr(tools_module.os, "open", record_open)
    monkeypatch.setattr(tools_module.os, "dup", record_dup)
    monkeypatch.setattr(tools_module.os, "listdir", reject_unbounded_listdir)
    result = await registry.execute(
        "scoped-list-cap", ToolCall(id="list", name="list_files", arguments={})
    )

    assert result.ok, result.error
    assert result.output == "... output truncated"
    assert "outside" not in result.output
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.asyncio
async def test_scoped_read_enforces_line_scan_and_persisted_output_budgets(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha = workspace.root / "alpha"
    alpha.mkdir()
    (alpha / "long-line.txt").write_text("x" * 200 + "\n")
    (alpha / "oversized.txt").write_text("x" * 1_000)
    (alpha / "many-lines.txt").write_text(("😀" * 7 + "\n") * 20)
    monkeypatch.setattr(tools_module, "SCOPED_READ_MAX_FILE_BYTES", 512)
    monkeypatch.setattr(tools_module, "SCOPED_READ_MAX_LINE_BYTES", 32)
    limited = replace(settings, model_output_limit=128, stored_output_limit=32)
    registry = _persisted_registry(
        storage,
        workspace,
        limited,
        RunRecord(id="scoped-read-cap", task="Inspect alpha", workspace=str(workspace.root)),
    )
    registry.bind_project_scope("scoped-read-cap", "alpha")

    huge_range = await registry.execute(
        "scoped-read-cap",
        ToolCall(
            id="range",
            name="read_file",
            arguments={"path": "many-lines.txt", "start_line": 1, "end_line": 1_000_000},
        ),
    )
    long_line = await registry.execute(
        "scoped-read-cap",
        ToolCall(id="long", name="read_file", arguments={"path": "long-line.txt"}),
    )
    oversized = await registry.execute(
        "scoped-read-cap",
        ToolCall(id="oversized", name="read_file", arguments={"path": "oversized.txt"}),
    )
    bounded = await registry.execute(
        "scoped-read-cap",
        ToolCall(
            id="bounded",
            name="read_file",
            arguments={"path": "many-lines.txt", "start_line": 1, "end_line": 1},
        ),
    )

    assert not huge_range.ok
    assert "at most" in (huge_range.error or "")
    assert not long_line.ok
    assert "requested line exceeds" in (long_line.error or "")
    assert not oversized.ok
    assert "read limit" in (oversized.error or "")
    assert bounded.ok, bounded.error
    assert bounded.output.endswith("... output truncated")
    assert len(bounded.output) <= limited.model_output_limit
    assert len(bounded.output.encode("utf-8")) <= limited.stored_output_limit


@pytest.mark.asyncio
async def test_scoped_read_drops_only_a_partial_utf8_tail_at_the_file_budget(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha = workspace.root / "alpha"
    alpha.mkdir()
    (alpha / "utf8.txt").write_text("first\n😀 trailing content\n")
    monkeypatch.setattr(tools_module, "SCOPED_READ_MAX_FILE_BYTES", 8)
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="scoped-utf8-cap", task="Inspect alpha", workspace=str(workspace.root)),
    )
    registry.bind_project_scope("scoped-utf8-cap", "alpha")

    result = await registry.execute(
        "scoped-utf8-cap",
        ToolCall(
            id="read",
            name="read_file",
            arguments={"path": "utf8.txt", "start_line": 1, "end_line": 1},
        ),
    )

    assert result.ok, result.error
    assert result.output == "     1 | first"


@pytest.mark.asyncio
async def test_scoped_recursive_search_and_reads_never_follow_file_symlinks(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
) -> None:
    alpha = workspace.root / "alpha"
    beta = workspace.root / "beta"
    alpha.mkdir()
    beta.mkdir()
    (alpha / "safe.txt").write_text("safe alpha payload\n")
    sensitive_content = "sibling secret reachable only through a file symlink"
    (beta / "secret.txt").write_text(sensitive_content)
    (alpha / "secret-link.txt").symlink_to(beta / "secret.txt")
    for ignored_name in ("node_modules", "build", "vendor"):
        ignored = alpha / ignored_name
        ignored.mkdir()
        (ignored / "dependency.txt").write_text(sensitive_content)
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="scoped-file-link", task="Inspect alpha", workspace=str(workspace.root)),
    )
    registry.bind_project_scope("scoped-file-link", "alpha")

    listed = await registry.execute(
        "scoped-file-link", ToolCall(id="list", name="list_files", arguments={})
    )
    read = await registry.execute(
        "scoped-file-link",
        ToolCall(
            id="read",
            name="read_file",
            arguments={"path": "secret-link.txt"},
        ),
    )
    searched = await registry.execute(
        "scoped-file-link",
        ToolCall(id="search", name="search_text", arguments={"query": "sibling secret"}),
    )

    assert listed.ok and "secret-link" not in listed.output
    assert all(name not in listed.output for name in ("node_modules", "build", "vendor"))
    assert not read.ok and "selected project scope" in (read.error or "")
    assert searched.ok and searched.output == "No matches"
    assert sensitive_content not in "\n".join(
        f"{result.output}\n{result.error}" for result in (listed, read, searched)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("git_name", [".git", ".GIT"])
async def test_scoped_listing_and_search_hide_regular_git_metadata_files(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    git_name: str,
) -> None:
    alpha = workspace.root / "alpha"
    alpha.mkdir()
    external_path = "/Users/alice/private/repository/.git/worktrees/alpha"
    (alpha / git_name).write_text(f"gitdir: {external_path}\n")
    (alpha / "README.md").write_text("safe project content\n")
    run_id = f"scoped-git-file-{git_name.casefold()}"
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id=run_id, task="Inspect alpha", workspace=str(workspace.root)),
    )
    registry.bind_project_scope(run_id, "alpha")

    listed = await registry.execute(
        run_id,
        ToolCall(id="list", name="list_files", arguments={}),
    )
    searched = await registry.execute(
        run_id,
        ToolCall(id="search", name="search_text", arguments={"query": "gitdir"}),
    )

    assert listed.ok, listed.error
    assert searched.ok, searched.error
    assert git_name not in listed.output
    assert searched.output == "No matches"
    assert external_path not in f"{listed.output}\n{searched.output}"


@pytest.mark.asyncio
async def test_scoped_search_invalid_regex_is_a_normal_tool_failure(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha = workspace.root / "alpha"
    alpha.mkdir()
    (alpha / "README.md").write_text("alpha\n")
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="scoped-invalid-regex", task="Inspect alpha", workspace=str(workspace.root)),
    )
    registry.bind_project_scope("scoped-invalid-regex", "alpha")
    real_open = tools_module.os.open
    root_descriptors: list[int] = []

    def record_root_open(path, flags, mode=0o777, *, dir_fd=None):
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if dir_fd is None and os.fspath(path) == os.fspath(alpha):
            root_descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(tools_module.os, "open", record_root_open)

    result = await registry.execute(
        "scoped-invalid-regex",
        ToolCall(id="search", name="search_text", arguments={"query": "["}),
    )

    assert not result.ok
    assert result.output == ""
    assert "Invalid regular expression" in (result.error or "")
    assert len(root_descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(root_descriptors[0])


@pytest.mark.asyncio
async def test_scoped_search_times_out_pathological_regex_as_a_tool_failure(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha = workspace.root / "alpha"
    alpha.mkdir()
    (alpha / "pathological.txt").write_text("a" * 10_000 + "!\n")
    monkeypatch.setattr(tools_module, "SEARCH_REGEX_TIMEOUT_SECONDS", 0.000_001)
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="scoped-regex-timeout", task="Inspect alpha", workspace=str(workspace.root)),
    )
    registry.bind_project_scope("scoped-regex-timeout", "alpha")

    result = await registry.execute(
        "scoped-regex-timeout",
        ToolCall(id="search", name="search_text", arguments={"query": "(a+)+$"}),
    )

    assert not result.ok
    assert result.output == ""
    assert result.error == "Regular expression search exceeded its time limit"


def test_search_matcher_caps_each_line_to_the_per_match_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_timeouts: list[float] = []

    class FastPattern:
        def search(self, _line: str, *, timeout: float, concurrent: bool):
            assert concurrent is True
            observed_timeouts.append(timeout)
            return None

    monkeypatch.setattr(tools_module, "SEARCH_REGEX_TIMEOUT_SECONDS", 0.125)
    monkeypatch.setattr(tools_module, "monotonic", lambda: 10.0)

    matched = tools_module._search_pattern_matches(
        FastPattern(),
        "ordinary text",
        deadline=20.0,
    )

    assert matched is False
    assert observed_timeouts == pytest.approx([0.125])


@pytest.mark.asyncio
async def test_search_enforces_one_cumulative_deadline_across_many_lines(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha = workspace.root / "alpha"
    alpha.mkdir()
    (alpha / "many-lines.txt").write_text("first\nsecond\nthird\n")
    clock = [0.0]
    observed_timeouts: list[float] = []

    class SlowPattern:
        def search(self, _line: str, *, timeout: float, concurrent: bool):
            assert concurrent is True
            observed_timeouts.append(timeout)
            work = 0.6
            if work >= timeout:
                clock[0] += timeout
                raise TimeoutError
            clock[0] += work
            return None

    monkeypatch.setattr(tools_module, "SEARCH_TOTAL_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(tools_module, "SEARCH_REGEX_TIMEOUT_SECONDS", 0.75)
    monkeypatch.setattr(tools_module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(tools_module, "_compile_search_pattern", lambda _query: SlowPattern())
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="search-total-timeout", task="Inspect alpha", workspace=str(workspace.root)),
    )
    registry.bind_project_scope("search-total-timeout", "alpha")

    result = await registry.execute(
        "search-total-timeout",
        ToolCall(id="search", name="search_text", arguments={"query": "anything"}),
    )

    assert not result.ok
    assert result.output == ""
    assert result.error == "Text search exceeded its overall time limit"
    assert observed_timeouts == pytest.approx([0.75, 0.4])


@pytest.mark.asyncio
async def test_scoped_search_rejects_an_unbounded_regex_query(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
) -> None:
    alpha = workspace.root / "alpha"
    alpha.mkdir()
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="scoped-regex-size", task="Inspect alpha", workspace=str(workspace.root)),
    )
    registry.bind_project_scope("scoped-regex-size", "alpha")

    result = await registry.execute(
        "scoped-regex-size",
        ToolCall(
            id="search",
            name="search_text",
            arguments={"query": "a" * (tools_module.SEARCH_MAX_QUERY_CHARS + 1)},
        ),
    )

    assert not result.ok
    assert "must be at most" in (result.error or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [False, True])
async def test_search_rejects_an_unbounded_glob_before_starting_the_scan(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
    scoped: bool,
) -> None:
    target = workspace.root / "alpha" if scoped else workspace.root
    if scoped:
        target.mkdir()
    (target / "README.md").write_text("needle\n")
    run_id = f"search-glob-size-{scoped}"
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id=run_id, task="Inspect", workspace=str(workspace.root)),
    )
    if scoped:
        registry.bind_project_scope(run_id, "alpha")

    def unexpected_scan(*_args, **_kwargs):
        raise AssertionError("search scan must not start for an oversized glob")

    monkeypatch.setattr(registry, "_search_text_scoped", unexpected_scan)
    result = await registry.execute(
        run_id,
        ToolCall(
            id="search",
            name="search_text",
            arguments={
                "query": "needle",
                "glob": "x" * (tools_module.SEARCH_MAX_GLOB_CHARS + 1),
            },
        ),
    )

    assert not result.ok
    assert result.output == ""
    assert result.error == (
        f"Glob must be at most {tools_module.SEARCH_MAX_GLOB_CHARS} characters"
    )


@pytest.mark.asyncio
async def test_search_compiles_glob_components_once_before_walking_files(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (workspace.root / "src").mkdir()
    (workspace.root / "nested" / "src").mkdir(parents=True)
    (workspace.root / "src" / "a.py").write_text("needle a\n")
    (workspace.root / "src" / "b.py").write_text("needle b\n")
    (workspace.root / "src" / "ignored.txt").write_text("needle text\n")
    (workspace.root / "nested" / "src" / "c.py").write_text("needle c\n")
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="search-glob-compiled", task="Inspect", workspace=str(workspace.root)),
    )
    real_compile = tools_module.re.compile
    compiled_components: list[str] = []

    def record_compile(pattern: str, *args, **kwargs):
        compiled_components.append(pattern)
        return real_compile(pattern, *args, **kwargs)

    monkeypatch.setattr(tools_module.re, "compile", record_compile)
    result = await registry.execute(
        "search-glob-compiled",
        ToolCall(
            id="search",
            name="search_text",
            arguments={"query": "needle", "glob": "src/*.py"},
        ),
    )

    assert result.ok, result.error
    assert "src/a.py" in result.output
    assert "src/b.py" in result.output
    assert "nested/src/c.py" in result.output
    assert "ignored.txt" not in result.output
    assert len(compiled_components) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "needle" + "x" * 32_000 + "\n",
        "needle on a short line\n" * 1_000,
    ],
)
async def test_scoped_search_hard_caps_a_single_long_match_before_persisting_it(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    content: str,
) -> None:
    alpha = workspace.root / "alpha"
    alpha.mkdir()
    (alpha / "matches.txt").write_text(content)
    limited = replace(settings, model_output_limit=128)
    registry = _persisted_registry(
        storage,
        workspace,
        limited,
        RunRecord(id="scoped-output-cap", task="Inspect alpha", workspace=str(workspace.root)),
    )
    registry.bind_project_scope("scoped-output-cap", "alpha")

    result = await registry.execute(
        "scoped-output-cap",
        ToolCall(id="search", name="search_text", arguments={"query": "needle"}),
    )

    assert result.ok, result.error
    assert "needle" in result.output
    assert result.output.endswith("... output truncated")
    assert len(result.output) <= limited.model_output_limit


@pytest.mark.asyncio
async def test_scoped_search_bounds_oversized_files_and_no_match_total_scan(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha = workspace.root / "alpha"
    alpha.mkdir()
    (alpha / "a-oversized.txt").write_text("hidden-needle " + "x" * 200)
    (alpha / "b.txt").write_text("ordinary text " + "y" * 50)
    (alpha / "c.txt").write_text("more ordinary text " + "z" * 50)
    monkeypatch.setattr(tools_module, "SCOPED_SEARCH_MAX_FILE_BYTES", 96)
    monkeypatch.setattr(tools_module, "SCOPED_SEARCH_MAX_TOTAL_BYTES", 100)
    monkeypatch.setattr(tools_module, "SCOPED_SEARCH_MAX_LINE_BYTES", 64)
    real_fdopen = tools_module.os.fdopen
    bytes_returned: list[int] = []

    class TrackingStream:
        def __init__(self, stream) -> None:
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            self.stream.close()

        def readline(self, size: int = -1):
            value = self.stream.readline(size)
            bytes_returned.append(len(value))
            return value

        def read(self, size: int = -1):
            value = self.stream.read(size)
            bytes_returned.append(len(value))
            return value

        def fileno(self) -> int:
            return self.stream.fileno()

    def track_fdopen(descriptor, *args, **kwargs):
        stream = real_fdopen(descriptor, *args, **kwargs)
        mode = kwargs.get("mode", args[0] if args else "r")
        return TrackingStream(stream) if "b" in mode else stream

    monkeypatch.setattr(tools_module.os, "fdopen", track_fdopen)
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="scoped-scan-cap", task="Inspect alpha", workspace=str(workspace.root)),
    )
    registry.bind_project_scope("scoped-scan-cap", "alpha")

    result = await registry.execute(
        "scoped-scan-cap",
        ToolCall(id="search", name="search_text", arguments={"query": "absent"}),
    )

    assert result.ok, result.error
    assert result.output == "... output truncated"
    assert "hidden-needle" not in result.output
    assert len(result.output) <= settings.model_output_limit
    assert sum(bytes_returned) <= tools_module.SCOPED_SEARCH_MAX_TOTAL_BYTES


@pytest.mark.asyncio
async def test_scoped_search_stops_opening_files_when_the_file_budget_is_exhausted(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha = workspace.root / "alpha"
    alpha.mkdir()
    (alpha / "a-first.txt").write_text("ordinary\n")
    (alpha / "z-never.txt").write_text("late needle\n")
    monkeypatch.setattr(tools_module, "SCOPED_SEARCH_MAX_FILES", 1)
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="scoped-file-cap", task="Inspect alpha", workspace=str(workspace.root)),
    )
    registry.bind_project_scope("scoped-file-cap", "alpha")
    real_open = tools_module.os.open
    opened_names: list[str] = []

    def record_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None:
            opened_names.append(os.fspath(path))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(tools_module.os, "open", record_open)
    result = await registry.execute(
        "scoped-file-cap",
        ToolCall(id="search", name="search_text", arguments={"query": "needle"}),
    )

    assert result.ok, result.error
    assert result.output == "... output truncated"
    assert "a-first.txt" in opened_names
    assert "z-never.txt" not in opened_names


@pytest.mark.asyncio
async def test_scoped_search_never_evaluates_a_line_larger_than_its_line_budget(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha = workspace.root / "alpha"
    alpha.mkdir()
    (alpha / "minified.js").write_text("needle" + "x" * 200)
    monkeypatch.setattr(tools_module, "SCOPED_SEARCH_MAX_FILE_BYTES", 512)
    monkeypatch.setattr(tools_module, "SCOPED_SEARCH_MAX_TOTAL_BYTES", 1_024)
    monkeypatch.setattr(tools_module, "SCOPED_SEARCH_MAX_LINE_BYTES", 64)
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="scoped-line-cap", task="Inspect alpha", workspace=str(workspace.root)),
    )
    registry.bind_project_scope("scoped-line-cap", "alpha")

    result = await registry.execute(
        "scoped-line-cap",
        ToolCall(id="search", name="search_text", arguments={"query": "needle"}),
    )

    assert result.ok, result.error
    assert result.output == "... output truncated"
    assert "needle" not in result.output


@pytest.mark.asyncio
async def test_scoped_filesystem_search_runs_off_the_event_loop(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha = workspace.root / "alpha"
    alpha.mkdir()
    (alpha / "README.md").write_text("alpha needle\n")
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="scoped-thread", task="Inspect alpha", workspace=str(workspace.root)),
    )
    registry.bind_project_scope("scoped-thread", "alpha")
    event_loop_thread = threading.get_ident()
    worker_threads: list[int] = []
    real_execute = registry._execute_scoped_read

    def record_execute(binding: tools_module.ProjectScopeBinding, call: ToolCall):
        worker_threads.append(threading.get_ident())
        registry.clear_project_scope("scoped-thread")
        return real_execute(binding, call)

    monkeypatch.setattr(registry, "_execute_scoped_read", record_execute)
    result = await registry.execute(
        "scoped-thread",
        ToolCall(id="search", name="search_text", arguments={"query": "needle"}),
    )

    assert result.ok, result.error
    assert "alpha needle" in result.output
    assert len(worker_threads) == 1
    assert worker_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_scoped_search_rejects_file_changed_to_symlink_between_stat_and_open(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha = workspace.root / "alpha"
    beta = workspace.root / "beta"
    alpha.mkdir()
    beta.mkdir()
    raced = alpha / "race.txt"
    raced.write_text("ordinary alpha content\n")
    sensitive_content = "sibling payload from a raced symlink"
    (beta / "secret.txt").write_text(sensitive_content)
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="scoped-link-race", task="Inspect alpha", workspace=str(workspace.root)),
    )
    registry.bind_project_scope("scoped-link-race", "alpha")
    real_open = tools_module.os.open
    swapped = False

    def swap_before_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "race.txt" and dir_fd is not None and not swapped:
            swapped = True
            raced.unlink()
            raced.symlink_to(beta / "secret.txt")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(tools_module.os, "open", swap_before_open)
    searched = await registry.execute(
        "scoped-link-race",
        ToolCall(id="search", name="search_text", arguments={"query": "sibling payload"}),
    )

    assert swapped is True
    assert not searched.ok
    assert "replaced after selection" in (searched.error or "")
    assert sensitive_content not in f"{searched.output}\n{searched.error}"


@pytest.mark.asyncio
async def test_bound_project_scope_fails_closed_if_scope_is_replaced_by_symlink(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
) -> None:
    alpha = workspace.root / "alpha"
    beta = workspace.root / "beta"
    alpha.mkdir()
    beta.mkdir()
    (alpha / "README.md").write_text("original alpha\n")
    sensitive_content = "replacement beta payload must stay hidden"
    (beta / "README.md").write_text(sensitive_content)
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="scope-swap", task="Inspect alpha", workspace=str(workspace.root)),
    )
    registry.bind_project_scope("scope-swap", "alpha")

    alpha.rename(workspace.root / "alpha-original")
    alpha.symlink_to(beta, target_is_directory=True)

    results = [
        await registry.execute(
            "scope-swap", ToolCall(id="list", name="list_files", arguments={})
        ),
        await registry.execute(
            "scope-swap",
            ToolCall(id="read", name="read_file", arguments={"path": "README.md"}),
        ),
        await registry.execute(
            "scope-swap",
            ToolCall(
                id="search",
                name="search_text",
                arguments={"query": "replacement beta payload"},
            ),
        ),
    ]

    for result in results:
        assert not result.ok
        assert sensitive_content not in f"{result.output}\n{result.error}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "hook_name"),
    [
        (ToolCall(id="list", name="list_files", arguments={}), "_open_scoped_directory"),
        (
            ToolCall(id="read", name="read_file", arguments={"path": "README.md"}),
            "_open_scoped_node",
        ),
        (
            ToolCall(id="search", name="search_text", arguments={"query": "replacement"}),
            "_open_scoped_node",
        ),
    ],
)
async def test_scoped_operations_discard_output_if_root_is_replaced_after_fd_open(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
    call: ToolCall,
    hook_name: str,
) -> None:
    alpha = workspace.root / "alpha"
    alpha.mkdir()
    (alpha / "README.md").write_text("original alpha\n")
    run_id = f"scope-replaced-during-{call.name}"
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id=run_id, task="Inspect alpha", workspace=str(workspace.root)),
    )
    registry.bind_project_scope(run_id, "alpha")
    original_hook = getattr(registry, hook_name)
    replacement_secret = "replacement content must be discarded"
    swapped = False

    def replace_after_scope_open(*args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            alpha.rename(workspace.root / "alpha-original")
            alpha.mkdir()
            (alpha / "README.md").write_text(replacement_secret)
            (alpha / "replacement-only.txt").write_text(replacement_secret)
        return original_hook(*args, **kwargs)

    monkeypatch.setattr(registry, hook_name, replace_after_scope_open)
    result = await registry.execute(run_id, call)

    assert swapped is True
    assert not result.ok
    assert "replaced after selection" in (result.error or "")
    assert replacement_secret not in f"{result.output}\n{result.error}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "hook_name"),
    [
        (
            ToolCall(id="list", name="list_files", arguments={}),
            "_open_scoped_directory",
        ),
        (
            ToolCall(id="read", name="read_file", arguments={"path": "README.md"}),
            "_open_scoped_node",
        ),
        (
            ToolCall(
                id="search",
                name="search_text",
                arguments={"query": "original|replacement"},
            ),
            "_open_scoped_node",
        ),
    ],
)
async def test_scoped_operations_discard_output_after_aba_root_swap(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
    call: ToolCall,
    hook_name: str,
) -> None:
    alpha = workspace.root / "alpha"
    alpha.mkdir()
    (alpha / "README.md").write_text("original alpha payload\n")
    run_id = f"scope-aba-{call.name}"
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id=run_id, task="Inspect alpha", workspace=str(workspace.root)),
    )
    registry.bind_project_scope(run_id, "alpha")
    original_hook = getattr(registry, hook_name)
    replacement_secret = "replacement ABA secret"
    swapped = False

    def aba_around_target_open(*args, **kwargs):
        nonlocal swapped
        if swapped:
            return original_hook(*args, **kwargs)
        swapped = True
        original_path = workspace.root / "alpha-original"
        replacement_path = workspace.root / "alpha-replacement"
        alpha.rename(original_path)
        alpha.mkdir()
        (alpha / "README.md").write_text(replacement_secret)
        (alpha / "replacement-only.txt").write_text(replacement_secret)
        try:
            opened = original_hook(*args, **kwargs)
        finally:
            alpha.rename(replacement_path)
            original_path.rename(alpha)
        return opened

    monkeypatch.setattr(registry, hook_name, aba_around_target_open)
    result = await registry.execute(run_id, call)

    assert swapped is True
    assert not result.ok
    assert "replaced after selection" in (result.error or "")
    assert replacement_secret not in f"{result.output}\n{result.error}"


@pytest.mark.asyncio
async def test_scoped_operations_close_temporary_root_descriptors(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha = workspace.root / "alpha"
    alpha.mkdir()
    (alpha / "README.md").write_text("alpha needle\n")
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="scope-fd-close", task="Inspect alpha", workspace=str(workspace.root)),
    )
    registry.bind_project_scope("scope-fd-close", "alpha")
    real_open = tools_module.os.open
    real_dup = tools_module.os.dup
    root_descriptors: list[int] = []
    all_descriptors: list[int] = []

    def record_root_open(path, flags, mode=0o777, *, dir_fd=None):
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        all_descriptors.append(descriptor)
        if dir_fd is None and os.fspath(path) == os.fspath(alpha):
            root_descriptors.append(descriptor)
        return descriptor

    def record_dup(descriptor: int) -> int:
        duplicate = real_dup(descriptor)
        all_descriptors.append(duplicate)
        return duplicate

    monkeypatch.setattr(tools_module.os, "open", record_root_open)
    monkeypatch.setattr(tools_module.os, "dup", record_dup)
    calls = [
        ToolCall(id="list", name="list_files", arguments={}),
        ToolCall(id="read", name="read_file", arguments={"path": "README.md"}),
        ToolCall(id="search", name="search_text", arguments={"query": "needle"}),
    ]
    for call in calls:
        descriptor_offset = len(all_descriptors)
        result = await registry.execute("scope-fd-close", call)
        assert result.ok, result.error
        descriptor = root_descriptors[-1]
        with pytest.raises(OSError):
            os.fstat(descriptor)
        for temporary_descriptor in all_descriptors[descriptor_offset:]:
            with pytest.raises(OSError):
                os.fstat(temporary_descriptor)

    assert len(root_descriptors) == len(calls)


def test_binary_stream_wrapper_closes_descriptor_if_fdopen_fails(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = workspace.root / "fdopen-failure.txt"
    path.write_text("content\n")
    descriptor = os.open(path, os.O_RDONLY)

    def reject_fdopen(*_args, **_kwargs):
        raise OSError("synthetic fdopen failure")

    monkeypatch.setattr(tools_module.os, "fdopen", reject_fdopen)
    with pytest.raises(OSError, match="synthetic fdopen failure"):
        with tools_module._binary_stream_from_fd(descriptor):
            raise AssertionError("unreachable")
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_persisted_project_scope_identity_rejects_replaced_directory_on_rebind(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
) -> None:
    alpha = workspace.root / "alpha"
    alpha.mkdir()
    (alpha / "README.md").write_text("original alpha\n")
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="scope-rebind", task="Inspect alpha", workspace=str(workspace.root)),
    )
    identity = registry.bind_project_scope("scope-rebind", "alpha")

    alpha.rename(workspace.root / "alpha-original")
    alpha.mkdir()
    (alpha / "README.md").write_text("replacement alpha\n")

    restarted_registry = ToolRegistry(workspace, settings)
    with pytest.raises(ValueError, match="replaced after it was persisted"):
        restarted_registry.bind_project_scope(
            "scope-rebind",
            "alpha",
            expected_identity=identity,
        )


def test_persisted_project_scope_identity_rejects_same_inode_with_new_ctime(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
) -> None:
    alpha = workspace.root / "alpha"
    alpha.mkdir()
    (alpha / "README.md").write_text("alpha\n")
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="scope-ctime", task="Inspect alpha", workspace=str(workspace.root)),
    )
    current_identity = registry.bind_project_scope("scope-ctime", "alpha")
    device, inode, ctime_ns = current_identity.split(":")
    prior_directory_instance = f"{device}:{inode}:{int(ctime_ns) + 1}"

    restarted_registry = ToolRegistry(workspace, settings)
    with pytest.raises(ValueError, match="replaced after it was persisted"):
        restarted_registry.bind_project_scope(
            "scope-ctime",
            "alpha",
            expected_identity=prior_directory_instance,
        )


@pytest.mark.asyncio
async def test_list_files_is_breadth_first_and_ignores_large_workspace_noise(
    settings: Settings,
    workspace: Workspace,
    storage: Storage,
) -> None:
    noisy = workspace.root / ".tmp_eval_sdk_install"
    noisy.mkdir()
    for index in range(1_050):
        (noisy / f"dependency-{index:04}.py").write_text("ignored\n")
    for ignored_name in (".idea", ".trae", "_tmp_clone"):
        ignored = workspace.root / ignored_name
        ignored.mkdir()
        (ignored / "package.json").write_text('{"name":"ignored"}\n')
    alpha = workspace.root / "alpha"
    zeta = workspace.root / "zeta"
    alpha.mkdir()
    zeta.mkdir()
    (alpha / "pyproject.toml").write_text("[project]\nname='alpha'\n")
    (zeta / "go.mod").write_text("module example.com/zeta\n")
    registry = _persisted_registry(
        storage,
        workspace,
        settings,
        RunRecord(id="breadth-first", task="Inspect", workspace=str(workspace.root)),
    )

    listed = await registry.execute(
        "breadth-first", ToolCall(id="list", name="list_files", arguments={})
    )

    assert listed.ok
    assert "alpha/" in listed.output and "zeta/" in listed.output
    assert "alpha/pyproject.toml" in listed.output
    assert "zeta/go.mod" in listed.output
    assert "output truncated" not in listed.output
    for ignored_name in (".tmp_eval_sdk_install", ".idea", ".trae", "_tmp_clone"):
        assert ignored_name not in listed.output


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

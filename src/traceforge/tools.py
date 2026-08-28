from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from traceforge.config import Settings
from traceforge.models import ApprovalMode, TaskPlan, ToolCall, ToolResult
from traceforge.patching import FilePatch, PatchError, apply_file_patch, parse_unified_diff
from traceforge.planning import is_safe_routine_check_variant
from traceforge.sandbox import CommandSandbox, SandboxStatus
from traceforge.workspace import Workspace, WorkspaceViolation, digest

OutputCallback = Callable[[str], Awaitable[None]]

SENSITIVE_ENV_NAME_PATTERN = re.compile(
    r"KEY|PASSWORD|PASSWD|PASSPHRASE|SECRET|TOKEN|CREDENTIAL",
    re.IGNORECASE,
)
SENSITIVE_ENV_EXACT = {"GPG_AGENT_INFO", "SSH_AUTH_SOCK"}


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class PermissionAssessment:
    decision: PermissionDecision
    reason: str
    risk: Literal["low", "unknown", "elevated", "dangerous"] = "low"


@dataclass(frozen=True, slots=True)
class PermissionResolution:
    mode: ApprovalMode
    decision: PermissionDecision
    reason: str
    risk: Literal["low", "unknown", "elevated", "dangerous"]
    policy_decision: PermissionDecision
    authorization: Literal["policy", "user", "full_access"]
    sandbox_bypass_on_allow: bool = False

    def as_metadata(self, *, outcome: str, sandbox_bypass: bool) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "policy_decision": self.policy_decision.value,
            "effective_decision": self.decision.value,
            "authorization": self.authorization,
            "outcome": outcome,
            "sandbox_bypass": sandbox_bypass,
            "reason": self.reason,
        }


class ToolRegistry:
    def __init__(self, workspace: Workspace, settings: Settings) -> None:
        self.workspace = workspace
        self.settings = settings
        self.sandbox = CommandSandbox(
            workspace.root, credential_file=settings.credential_file
        )
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    @property
    def sandbox_status(self) -> SandboxStatus:
        return self.sandbox.status

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [
            _tool_schema(
                "list_files",
                "List files and directories inside the workspace.",
                {
                    "path": {"type": "string", "default": "."},
                    "max_depth": {"type": "integer", "minimum": 1, "maximum": 6, "default": 3},
                },
            ),
            _tool_schema(
                "read_file",
                "Read a UTF-8 text file with line numbers.",
                {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1, "default": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                required=["path"],
            ),
            _tool_schema(
                "search_text",
                "Search text in workspace files. The query is a regular expression.",
                {
                    "query": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "glob": {"type": "string"},
                },
                required=["query"],
            ),
            _tool_schema(
                "apply_patch",
                (
                    "Apply a unified diff or Begin/Update File patch transactionally inside "
                    "the workspace. Hunk counts are repaired, but context must match uniquely."
                ),
                {"patch": {"type": "string"}},
                required=["patch"],
            ),
            _tool_schema(
                "create_file",
                "Create a new UTF-8 file. Existing files are never overwritten.",
                {"path": {"type": "string"}, "content": {"type": "string"}},
                required=["path", "content"],
            ),
            _tool_schema(
                "run_command",
                "Run one program without a shell, inside the selected workspace.",
                {
                    "argv": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 40,
                    },
                    "cwd": {"type": "string", "default": "."},
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": self.settings.max_command_timeout,
                        "default": self.settings.command_timeout,
                    },
                },
                required=["argv"],
            ),
            _tool_schema(
                "finish",
                "Request completion after all accepted checks have current evidence.",
                {
                    "summary": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                },
                required=["summary", "evidence"],
            ),
        ]

    def assess(self, call: ToolCall, plan: TaskPlan | None) -> PermissionAssessment:
        if call.name in {"apply_patch", "create_file"} and plan and plan.impacted_files:
            try:
                mutation_paths = self._mutation_paths(call)
            except (PatchError, WorkspaceViolation) as exc:
                return PermissionAssessment(PermissionDecision.DENY, str(exc), "dangerous")
            unexpected = sorted(set(mutation_paths) - set(plan.impacted_files))
            if unexpected:
                return PermissionAssessment(
                    PermissionDecision.ASK,
                    "Mutation exceeds the visible plan scope: " + ", ".join(unexpected),
                    "elevated",
                )
            return PermissionAssessment(
                PermissionDecision.ALLOW, "Mutation stays inside the visible plan scope"
            )
        if call.name != "run_command":
            return PermissionAssessment(PermissionDecision.ALLOW, "Workspace tool")
        argv = call.arguments.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) for item in argv)
        ):
            return PermissionAssessment(
                PermissionDecision.DENY,
                "argv must be a non-empty string list",
                "unknown",
            )
        hard_denial = _hard_command_denial(argv, self.workspace.root)
        if hard_denial is not None:
            return PermissionAssessment(
                PermissionDecision.DENY,
                hard_denial,
                "dangerous",
            )
        check_relation = _accepted_check_relation(argv, plan)
        if check_relation == "exact":
            return PermissionAssessment(
                PermissionDecision.ALLOW, "Command is an approved acceptance check"
            )
        if check_relation == "routine_variant":
            return PermissionAssessment(
                PermissionDecision.ALLOW,
                "Command is a sandboxed variant of an approved routine check",
            )
        if _is_read_only(argv):
            return PermissionAssessment(PermissionDecision.ALLOW, "Known read-only command")
        return PermissionAssessment(
            PermissionDecision.ASK,
            "The command can execute project code, modify files, access the network, "
            "or write externally",
            "elevated",
        )

    def resolve_permission(
        self,
        call: ToolCall,
        plan: TaskPlan | None,
        mode: ApprovalMode,
    ) -> PermissionResolution:
        """Apply the user-selected approval mode without weakening invariant denials."""

        assessment = self.assess(call, plan)
        if assessment.decision is PermissionDecision.DENY:
            return PermissionResolution(
                mode=mode,
                decision=PermissionDecision.DENY,
                reason=assessment.reason,
                risk=assessment.risk,
                policy_decision=assessment.decision,
                authorization="policy",
            )
        if mode is ApprovalMode.MANUAL and call.name in {
            "apply_patch",
            "create_file",
            "run_command",
        }:
            return PermissionResolution(
                mode=mode,
                decision=PermissionDecision.ASK,
                reason=(
                    "Manual approval mode requires confirmation before every edit or command. "
                    + assessment.reason
                ),
                risk=assessment.risk,
                policy_decision=assessment.decision,
                authorization="user",
                sandbox_bypass_on_allow=False,
            )
        if mode is ApprovalMode.FULL_ACCESS:
            if (
                call.name == "run_command"
                and assessment.decision is PermissionDecision.ASK
                and not self.sandbox.status.enforced
            ):
                return PermissionResolution(
                    mode=mode,
                    decision=PermissionDecision.ASK,
                    reason=(
                        "Full access cannot auto-approve an unclassified command because "
                        "no OS sandbox is available. "
                        + assessment.reason
                    ),
                    risk=assessment.risk,
                    policy_decision=assessment.decision,
                    authorization="user",
                    sandbox_bypass_on_allow=True,
                )
            return PermissionResolution(
                mode=mode,
                decision=PermissionDecision.ALLOW,
                reason=(
                    "Full access mode allows this workspace action without prompting while "
                    "retaining invariant workspace and OS-sandbox boundaries. "
                    + assessment.reason
                ),
                risk=assessment.risk,
                policy_decision=assessment.decision,
                authorization="full_access",
                sandbox_bypass_on_allow=False,
            )
        return PermissionResolution(
            mode=mode,
            decision=assessment.decision,
            reason=assessment.reason,
            risk=assessment.risk,
            policy_decision=assessment.decision,
            authorization=(
                "user"
                if assessment.decision is PermissionDecision.ASK
                else "policy"
            ),
            sandbox_bypass_on_allow=(
                call.name == "run_command"
                and assessment.decision is PermissionDecision.ASK
            ),
        )

    async def execute(
        self,
        run_id: str,
        call: ToolCall,
        *,
        output_callback: OutputCallback | None = None,
        sandbox_bypass: bool = False,
    ) -> ToolResult:
        mutation_baseline: dict[str, tuple[bool, str | None]] = {}
        try:
            if call.name in {"apply_patch", "create_file"}:
                mutation_baseline = self._mutation_baseline(call)
            if call.name == "list_files":
                output = self._list_files(**call.arguments)
            elif call.name == "read_file":
                output = self._read_file(**call.arguments)
            elif call.name == "search_text":
                output = await self._search_text(**call.arguments)
            elif call.name == "apply_patch":
                output = self._apply_patch(run_id, **call.arguments)
            elif call.name == "create_file":
                output = self._create_file(run_id, **call.arguments)
            elif call.name == "run_command":
                return await self._run_command(
                    run_id,
                    call,
                    output_callback=output_callback,
                    sandbox_bypass=sandbox_bypass,
                    **call.arguments,
                )
            else:
                return ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    ok=False,
                    error=f"Unknown or non-executable tool: {call.name}",
                )
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                ok=True,
                output=output,
                metadata=(
                    {"changed_files": self._changed_mutation_paths(mutation_baseline)}
                    if call.name in {"apply_patch", "create_file"}
                    else {}
                ),
            )
        except (OSError, UnicodeError, ValueError, PatchError, WorkspaceViolation) as exc:
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                ok=False,
                error=str(exc),
                metadata=(
                    {"changed_files": self._changed_mutation_paths(mutation_baseline)}
                    if call.name in {"apply_patch", "create_file"}
                    else {}
                ),
            )

    def _mutation_paths(self, call: ToolCall) -> list[str]:
        if call.name == "create_file":
            raw = call.arguments.get("path")
            if not isinstance(raw, str) or not raw.strip():
                raise PatchError("create_file path must be a non-empty string")
            return [self.workspace.relative(self.workspace.resolve_write(raw, must_exist=False))]
        raw_patch = call.arguments.get("patch")
        if not isinstance(raw_patch, str):
            raise PatchError("apply_patch patch must be a string")
        paths: list[str] = []
        for file_patch in parse_unified_diff(raw_patch):
            path = self.workspace.resolve_write(file_patch.path)
            if (
                file_patch.new_path is not None
                and file_patch.old_path is not None
                and file_patch.new_path != file_patch.old_path
            ):
                raise PatchError("File renames are not supported in v1")
            paths.append(self.workspace.relative(path))
        return sorted(set(paths))

    def _mutation_baseline(self, call: ToolCall) -> dict[str, tuple[bool, str | None]]:
        baseline: dict[str, tuple[bool, str | None]] = {}
        for relative_path in self._mutation_paths(call):
            path = self.workspace.resolve_write(relative_path)
            baseline[relative_path] = _file_fingerprint(path)
        return baseline

    def _changed_mutation_paths(
        self, baseline: dict[str, tuple[bool, str | None]]
    ) -> list[str]:
        return [
            relative_path
            for relative_path, before in baseline.items()
            if _file_fingerprint(self.workspace.resolve_write(relative_path)) != before
        ]

    async def cancel(self, run_id: str) -> None:
        process = self._processes.get(run_id)
        if process is None or process.returncode is not None:
            return
        os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except TimeoutError:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()

    def _list_files(self, path: str = ".", max_depth: int = 3) -> str:
        root = self.workspace.resolve_read(path)
        if not root.is_dir():
            raise ValueError(f"Not a directory: {path}")
        ignored = {".git", ".venv", "node_modules", "__pycache__", "dist", "build"}
        rows: list[str] = []
        root_depth = len(root.parts)
        for current, directories, files in os.walk(root):
            current_path = Path(current)
            depth = len(current_path.parts) - root_depth
            directories[:] = sorted(item for item in directories if item not in ignored)
            if depth >= max_depth:
                directories[:] = []
            relative_dir = self.workspace.relative(current_path)
            if relative_dir != ".":
                rows.append(f"{relative_dir}/")
            rows.extend(
                self.workspace.relative(current_path / name)
                for name in sorted(files)
                if not _is_secret_file(name)
            )
            if len(rows) >= 1_000:
                rows.append("... output truncated at 1,000 entries")
                break
        return "\n".join(rows) or "(empty directory)"

    def _read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        file_path = self.workspace.resolve_read(path)
        if not file_path.is_file():
            raise ValueError(f"Not a file: {path}")
        if _is_secret_file(file_path.name):
            raise ValueError("Secret-bearing environment files cannot be read by the agent")
        content = file_path.read_text(encoding="utf-8")
        lines = content.splitlines()
        if end_line is None:
            end_line = min(start_line + 399, len(lines))
        if end_line < start_line:
            raise ValueError("end_line must not be before start_line")
        selected = lines[start_line - 1 : end_line]
        return "\n".join(
            f"{number:>6} | {line}" for number, line in enumerate(selected, start=start_line)
        )

    async def _search_text(self, query: str, path: str = ".", glob: str | None = None) -> str:
        target = self.workspace.resolve_read(path)
        argv = ["rg", "-n", "--no-heading", "--color", "never"]
        argv.extend(["--glob", "!.env", "--glob", "!.env.*", "--glob", ".env.example"])
        if glob:
            argv.extend(["--glob", glob])
        argv.extend([query, str(target)])
        if shutil.which("rg"):
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            if process.returncode not in {0, 1}:
                raise ValueError(stderr.decode(errors="replace"))
            output = stdout.decode(errors="replace")
            return output[: self.settings.model_output_limit] or "No matches"
        expression = re.compile(query)
        matches: list[str] = []
        files = [target] if target.is_file() else target.rglob(glob or "*")
        for file_path in files:
            if not file_path.is_file() or ".git" in file_path.parts:
                continue
            if _is_secret_file(file_path.name):
                continue
            try:
                for number, line in enumerate(file_path.read_text().splitlines(), start=1):
                    if expression.search(line):
                        matches.append(f"{self.workspace.relative(file_path)}:{number}:{line}")
                        if len("\n".join(matches)) >= self.settings.model_output_limit:
                            return "\n".join(matches) + "\n... output truncated"
            except (UnicodeDecodeError, OSError):
                continue
        return "\n".join(matches) or "No matches"

    def _apply_patch(self, run_id: str, patch: str) -> str:
        patches = parse_unified_diff(patch)
        planned: list[tuple[FilePatch, Path, str | None]] = []
        for file_patch in patches:
            if file_patch.old_path is None:
                path = self.workspace.resolve_write(file_patch.path, must_exist=False)
                original = ""
            else:
                path = self.workspace.resolve_write(file_patch.old_path, must_exist=True)
                original = path.read_text(encoding="utf-8")
            if file_patch.new_path is None:
                rendered = None
            else:
                if file_patch.old_path and file_patch.new_path != file_patch.old_path:
                    raise PatchError("File renames are not supported in v1")
                rendered = apply_file_patch(original, file_patch)
            planned.append((file_patch, path, rendered))
        for _file_patch, path, rendered in planned:
            self.workspace.snapshot(run_id, path)
            if rendered is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(rendered, encoding="utf-8")
            self.workspace.record_agent_version(run_id, path)
        return self.workspace.diff(run_id)

    def _create_file(self, run_id: str, path: str, content: str) -> str:
        target = self.workspace.resolve_write(path, must_exist=False)
        self.workspace.snapshot(run_id, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.workspace.record_agent_version(run_id, target)
        return self.workspace.diff(run_id)

    async def _run_command(
        self,
        run_id: str,
        call: ToolCall,
        argv: list[str],
        cwd: str = ".",
        timeout_seconds: int | None = None,
        output_callback: OutputCallback | None = None,
        sandbox_bypass: bool = False,
    ) -> ToolResult:
        if not argv or not all(isinstance(item, str) and item for item in argv):
            raise ValueError("argv must contain non-empty strings")
        hard_denial = _hard_command_denial(argv, self.workspace.root)
        if hard_denial is not None:
            raise ValueError(hard_denial)
        command_temp = tempfile.TemporaryDirectory(prefix="traceforge-command-")
        command_temp_path = Path(command_temp.name)
        sandbox_home = command_temp_path / "home"
        sandbox_tmp = command_temp_path / "tmp"
        sandbox_cache = command_temp_path / "cache"
        try:
            for directory in (sandbox_home, sandbox_tmp, sandbox_cache):
                directory.mkdir()
            environment = _command_environment(
                self.workspace.root,
                home=sandbox_home,
                temp=sandbox_tmp,
                cache=sandbox_cache,
            )
            executable = (
                shutil.which(argv[0], path=environment["PATH"])
                if not Path(argv[0]).is_absolute()
                else argv[0]
            )
            if executable is None:
                raise ValueError(f"Executable not found: {argv[0]}")
            command_cwd = self.workspace.resolve_read(cwd)
            if not command_cwd.is_dir():
                raise ValueError(f"Command cwd is not a directory: {cwd}")
            timeout = timeout_seconds or self.settings.command_timeout
            if timeout > self.settings.max_command_timeout:
                raise ValueError(f"timeout_seconds exceeds {self.settings.max_command_timeout}")
            launch = self.sandbox.prepare(
                executable,
                argv,
                cwd=command_cwd,
                command_temp=command_temp_path,
                environment=environment,
                bypass=sandbox_bypass,
            )
            process = await asyncio.create_subprocess_exec(
                launch.program,
                *launch.arguments,
                cwd=command_cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                env=environment,
            )
        except BaseException:
            command_temp.cleanup()
            raise
        self._processes[run_id] = process
        chunks: list[bytes] = []
        stored_size = 0
        truncated = False
        stdout = process.stdout
        assert stdout is not None
        try:

            async def consume() -> None:
                nonlocal stored_size, truncated
                while chunk := await stdout.read(4096):
                    if output_callback:
                        await output_callback(chunk.decode(errors="replace"))
                    remaining = self.settings.stored_output_limit - stored_size
                    if remaining > 0:
                        chunks.append(chunk[:remaining])
                        stored_size += min(len(chunk), remaining)
                    if len(chunk) > remaining:
                        truncated = True

            await asyncio.wait_for(asyncio.gather(consume(), process.wait()), timeout=timeout)
        except TimeoutError:
            await self.cancel(run_id)
            output = b"".join(chunks).decode(errors="replace")
            if truncated:
                output += (
                    f"\n... output truncated at {self.settings.stored_output_limit} bytes ...\n"
                )
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                ok=False,
                output=output,
                error=f"Command timed out after {timeout} seconds",
                metadata={
                    "timeout": True,
                    "truncated": truncated,
                    "argv": argv,
                    "sandbox": launch.metadata,
                },
            )
        finally:
            self._processes.pop(run_id, None)
            command_temp.cleanup()
        output = b"".join(chunks).decode(errors="replace")
        if truncated:
            output += f"\n... output truncated at {self.settings.stored_output_limit} bytes ...\n"
        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            ok=process.returncode == 0,
            output=output,
            error=None if process.returncode == 0 else f"Command exited with {process.returncode}",
            metadata={
                "exit_code": process.returncode,
                "truncated": truncated,
                "argv": argv,
                "cwd": self.workspace.relative(command_cwd),
                "sandbox": launch.metadata,
            },
        )


def _tool_schema(
    name: str,
    description: str,
    properties: dict[str, Any],
    *,
    required: list[str] | None = None,
) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        parameters["required"] = required
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


def _accepted_check_relation(
    argv: list[str], plan: TaskPlan | None
) -> Literal["exact", "routine_variant"] | None:
    if plan is None:
        return None
    commands = [check.command for check in plan.acceptance_checks if check.command]
    if argv in commands:
        return "exact"
    if any(is_safe_routine_check_variant(argv, command) for command in commands):
        return "routine_variant"
    return None


def _file_fingerprint(path: Path) -> tuple[bool, str | None]:
    if not path.exists():
        return False, None
    return True, digest(path.read_bytes())


def _is_read_only(argv: list[str]) -> bool:
    executable = Path(argv[0]).name
    if executable in {"rg", "pwd", "ls", "cat", "head", "tail", "wc"}:
        return True
    return executable == "git" and len(argv) > 1 and argv[1] in {
        "status",
        "diff",
        "log",
        "show",
        "grep",
        "ls-files",
    }


def _outside_workspace_argument(argv: list[str], workspace: Path) -> str | None:
    for raw in argv[1:]:
        candidate = raw.split("=", 1)[1] if raw.startswith("-") and "=" in raw else raw
        if not candidate or candidate.startswith("-"):
            continue
        path = Path(candidate).expanduser()
        if not path.is_absolute() and ".." not in path.parts:
            continue
        resolved = path.resolve() if path.is_absolute() else (workspace / path).resolve()
        if not resolved.is_relative_to(workspace):
            return candidate
    return None


def _hard_command_denial(argv: list[str], workspace: Path) -> str | None:
    executable = Path(argv[0]).name
    if executable in {"sudo", "su", "shutdown", "reboot", "mkfs", "diskutil", "dd"}:
        return f"{executable} is outside TraceForge's safety boundary"
    if _is_dangerous_git(argv) or _is_dangerous_remove(argv):
        return "Destructive version-control or recursive deletion command"
    outside_path = _outside_workspace_argument(argv, workspace)
    if outside_path is not None:
        return f"Command path is outside the workspace: {outside_path}"
    return None


def scrubbed_environment() -> dict[str, str]:
    """Return the process environment without credential-like variables."""

    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in SENSITIVE_ENV_EXACT
        and SENSITIVE_ENV_NAME_PATTERN.search(key) is None
    }


def _command_environment(
    workspace: Path, *, home: Path, temp: Path, cache: Path
) -> dict[str, str]:
    environment = scrubbed_environment()
    runtime_dirs = [
        workspace / ".venv" / "bin",
        workspace / "venv" / "bin",
        Path(sys.executable).parent,
    ]
    existing = environment.get("PATH", "").split(os.pathsep)
    ordered = [str(path) for path in runtime_dirs if path.is_dir()]
    ordered.extend(path for path in existing if path and path not in ordered)
    environment["PATH"] = os.pathsep.join(ordered)
    environment.update(
        {
            "HOME": str(home),
            "TMPDIR": str(temp),
            "TMP": str(temp),
            "TEMP": str(temp),
            "XDG_CACHE_HOME": str(cache),
            "UV_CACHE_DIR": str(cache / "uv"),
            "npm_config_cache": str(cache / "npm"),
        }
    )
    return environment


def _is_dangerous_git(argv: list[str]) -> bool:
    return len(argv) > 2 and Path(argv[0]).name == "git" and argv[1:3] == ["reset", "--hard"]


def _is_dangerous_remove(argv: list[str]) -> bool:
    if Path(argv[0]).name != "rm":
        return False
    flags = "".join(item for item in argv[1:] if item.startswith("-"))
    return "r" in flags and "f" in flags


def _is_secret_file(name: str) -> bool:
    return name == ".env" or (name.startswith(".env.") and name != ".env.example")

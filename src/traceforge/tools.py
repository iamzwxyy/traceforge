from __future__ import annotations

import asyncio
import fnmatch
import hmac
import os
import re
import shlex
import shutil
import signal
import stat
import sys
import tempfile
from collections import deque
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import Any, BinaryIO, Literal, cast

import regex

from traceforge.config import Settings
from traceforge.models import ApprovalMode, FinishRequest, TaskPlan, ToolCall, ToolResult
from traceforge.patching import FilePatch, PatchError, apply_file_patch, parse_unified_diff
from traceforge.planning import is_safe_routine_check_variant
from traceforge.project_scope import is_ignored_workspace_directory
from traceforge.sandbox import CommandSandbox, SandboxStatus
from traceforge.streaming import contains_redactable_secret
from traceforge.workspace import Workspace, WorkspaceViolation, digest

OutputCallback = Callable[[str], Awaitable[None]]

SENSITIVE_ENV_NAME_PATTERN = re.compile(
    r"KEY|PASSWORD|PASSWD|PASSPHRASE|SECRET|TOKEN|CREDENTIAL",
    re.IGNORECASE,
)
SENSITIVE_ENV_EXACT = {"GPG_AGENT_INFO", "SSH_AUTH_SOCK"}

# Scoped search never hands an unbounded workspace file to Python's regular-expression
# engine. These limits are deliberately independent from the result-size limit: a search
# with no matches still needs a finite amount of local work.
SCOPED_SEARCH_MAX_FILE_BYTES = 2 * 1024 * 1024
SCOPED_SEARCH_MAX_TOTAL_BYTES = 16 * 1024 * 1024
SCOPED_SEARCH_MAX_LINE_BYTES = 64 * 1024
SCOPED_SEARCH_MAX_FILES = 10_000
SCOPED_SEARCH_MAX_ENTRIES = 20_000
SEARCH_MAX_QUERY_CHARS = 4_096
SEARCH_MAX_GLOB_CHARS = 1_024
SEARCH_REGEX_TIMEOUT_SECONDS = 0.05
SEARCH_TOTAL_TIMEOUT_SECONDS = 2.0
SCOPED_LIST_MAX_ENTRIES = 2_000
SCOPED_LIST_MAX_ROWS = 1_000
SCOPED_READ_MAX_FILE_BYTES = 2 * 1024 * 1024
SCOPED_READ_MAX_LINE_BYTES = 256 * 1024
SCOPED_READ_MAX_LINES = 400
SCOPED_READ_CHUNK_BYTES = 64 * 1024


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class ProjectScopeBinding:
    relative_path: str
    resolved_path: Path
    device: int
    inode: int
    ctime_ns: int | None


def _project_identity_token(identity: os.stat_result) -> str:
    return f"{identity.st_dev}:{identity.st_ino}:{identity.st_ctime_ns}"


def _binding_matches_identity(
    binding: ProjectScopeBinding,
    identity: os.stat_result,
) -> bool:
    return (identity.st_dev, identity.st_ino) == (binding.device, binding.inode) and (
        binding.ctime_ns is None or identity.st_ctime_ns == binding.ctime_ns
    )


def _binding_identity_token(binding: ProjectScopeBinding) -> str:
    if binding.ctime_ns is None:
        raise ValueError("Persisted project scope identity requires a ctime generation")
    return f"{binding.device}:{binding.inode}:{binding.ctime_ns}"


@dataclass(frozen=True, slots=True)
class SearchGlob:
    components: tuple[re.Pattern[str], ...]

    def matches(self, path_components: tuple[str, ...]) -> bool:
        if len(path_components) < len(self.components):
            return False
        candidate = path_components[-len(self.components) :]
        return all(
            pattern.fullmatch(component) is not None
            for pattern, component in zip(self.components, candidate, strict=True)
        )


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
        self.workspace.storage.register_credential_guard(settings.api_key)
        self.sandbox = CommandSandbox(
            workspace.root, credential_file=settings.credential_file
        )
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._instruction_grants: dict[str, str] = {}
        self._project_scopes: dict[str, ProjectScopeBinding] = {}
        workspace_identity = self.workspace.root.stat(follow_symlinks=False)
        self._workspace_read_scope = ProjectScopeBinding(
            relative_path=".",
            resolved_path=self.workspace.root,
            device=workspace_identity.st_dev,
            inode=workspace_identity.st_ino,
            # The unscoped workspace root legitimately changes as tools create top-level
            # entries. Persisted project selections below always bind the ctime generation.
            ctime_ns=None,
        )

    def bind_workspace_instruction_snapshot(
        self,
        run_id: str,
        snapshot_sha256: str,
    ) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", snapshot_sha256):
            raise ValueError("Workspace instruction snapshot SHA-256 is invalid")
        self._instruction_grants[run_id] = snapshot_sha256

    def clear_workspace_instruction_snapshot(self, run_id: str) -> None:
        self._instruction_grants.pop(run_id, None)

    def bind_project_scope(
        self,
        run_id: str,
        relative_path: str,
        *,
        expected_identity: str | None = None,
    ) -> str:
        lexical = self.workspace.root / relative_path
        if lexical.is_symlink():
            raise ValueError("Selected project scope cannot be a symlink")
        resolved = self.workspace.resolve_read(relative_path)
        if not resolved.is_dir():
            raise ValueError("Selected project scope must be a directory")
        if self.workspace.relative(resolved) != relative_path:
            raise ValueError("Selected project scope must resolve to its recorded path")
        identity = lexical.stat(follow_symlinks=False)
        identity_token = _project_identity_token(identity)
        if expected_identity is not None and not hmac.compare_digest(
            identity_token,
            expected_identity,
        ):
            raise ValueError("Selected project scope was replaced after it was persisted")
        self._project_scopes[run_id] = ProjectScopeBinding(
            relative_path=relative_path,
            resolved_path=resolved,
            device=identity.st_dev,
            inode=identity.st_ino,
            ctime_ns=identity.st_ctime_ns,
        )
        return identity_token

    def clear_project_scope(self, run_id: str) -> None:
        self._project_scopes.pop(run_id, None)

    def current_project_scope_identity(self, run_id: str) -> str | None:
        binding = self._project_scopes.get(run_id)
        if binding is None:
            return None
        self._validated_project_scope(binding)
        return _binding_identity_token(binding)

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
                    "query": {"type": "string", "maxLength": SEARCH_MAX_QUERY_CHARS},
                    "path": {"type": "string", "default": "."},
                    "glob": {"type": "string", "maxLength": SEARCH_MAX_GLOB_CHARS},
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
            {
                "type": "function",
                "function": {
                    "name": "finish",
                    "description": (
                        "Request completion in a model turn containing no other tool calls, after "
                        "all accepted command checks have current passing results."
                    ),
                    "parameters": FinishRequest.model_json_schema(),
                },
            },
        ]

    def assess(
        self,
        call: ToolCall,
        plan: TaskPlan | None,
        *,
        run_id: str | None = None,
    ) -> PermissionAssessment:
        binding = self._project_scopes.get(run_id) if run_id is not None else None
        if binding is not None:
            try:
                self._validated_project_scope(binding)
            except WorkspaceViolation as exc:
                return PermissionAssessment(
                    PermissionDecision.DENY,
                    str(exc),
                    "dangerous",
                )
        if (
            call.name in {"apply_patch", "create_file"}
            and plan is not None
            and not plan.impacted_files
        ):
            return PermissionAssessment(
                PermissionDecision.DENY,
                "Mutation denied because the visible plan declares no impacted files",
                "dangerous",
            )
        if call.name in {"apply_patch", "create_file"} and plan and plan.impacted_files:
            try:
                mutation_paths = self._mutation_paths(call, binding=binding)
                planned_paths = {
                    self._effective_scoped_path(binding, path)
                    if binding is not None
                    else self.workspace.relative(
                        self.workspace.resolve_write(path)
                    )
                    for path in plan.impacted_files
                }
            except (PatchError, WorkspaceViolation) as exc:
                return PermissionAssessment(PermissionDecision.DENY, str(exc), "dangerous")
            unexpected = sorted(set(mutation_paths) - planned_paths)
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
        command_root = (
            binding.resolved_path if binding is not None else self.workspace.root
        )
        hard_denial = _hard_command_denial(argv, command_root)
        if hard_denial is not None:
            return PermissionAssessment(
                PermissionDecision.DENY,
                hard_denial,
                "dangerous",
            )
        try:
            self.sandbox.validate_executable_scope(
                argv[0],
                execution_root=command_root,
            )
        except ValueError as exc:
            return PermissionAssessment(
                PermissionDecision.DENY,
                str(exc),
                "dangerous",
            )
        if binding is not None and not self.sandbox.status.enforced:
            return PermissionAssessment(
                PermissionDecision.DENY,
                "Commands in a selected project require an enforced OS sandbox",
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
        *,
        run_id: str | None = None,
    ) -> PermissionResolution:
        """Apply the user-selected approval mode without weakening invariant denials."""

        assessment = self.assess(call, plan, run_id=run_id)
        scoped = run_id is not None and run_id in self._project_scopes
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
                and not scoped
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
        scope_metadata: dict[str, str] = {}
        try:
            binding = self._project_scopes.get(run_id)
            if binding is not None:
                self._validated_project_scope(binding)
                scope_metadata = self._project_scope_metadata(binding)
            if call.name in {"apply_patch", "create_file", "run_command"}:
                self._require_workspace_instruction_grant(run_id)
            if call.name in {"apply_patch", "create_file"}:
                mutation_baseline = self._mutation_baseline(call, binding=binding)
            if call.name in {"list_files", "read_file", "search_text"}:
                # Every read uses the same bounded descriptor walker. A selected project narrows
                # the virtual root; otherwise the immutable workspace root binding is used without
                # reporting a synthetic project scope to the UI.
                if binding is not None:
                    output, scope_metadata = await asyncio.to_thread(
                        self._execute_scoped_read,
                        binding,
                        call,
                    )
                else:
                    output, scope_metadata = await asyncio.to_thread(
                        self._execute_workspace_read,
                        call,
                    )
            elif call.name == "apply_patch":
                try:
                    output = self._apply_patch(run_id, binding=binding, **call.arguments)
                finally:
                    if binding is not None:
                        binding = self._refresh_project_scope_binding(run_id, binding)
                        scope_metadata = self._project_scope_metadata(binding)
            elif call.name == "create_file":
                try:
                    output = self._create_file(run_id, binding=binding, **call.arguments)
                finally:
                    if binding is not None:
                        binding = self._refresh_project_scope_binding(run_id, binding)
                        scope_metadata = self._project_scope_metadata(binding)
            elif call.name == "run_command":
                return await self._run_command(
                    run_id,
                    call,
                    binding=binding,
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
                    {
                        "changed_files": self._changed_mutation_paths(mutation_baseline),
                        **scope_metadata,
                    }
                    if call.name in {"apply_patch", "create_file"}
                    else scope_metadata
                ),
            )
        except (OSError, UnicodeError, ValueError, PatchError, WorkspaceViolation) as exc:
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                ok=False,
                error=str(exc),
                metadata=(
                    {
                        "changed_files": self._changed_mutation_paths(mutation_baseline),
                        **scope_metadata,
                    }
                    if call.name in {"apply_patch", "create_file"}
                    else scope_metadata
                ),
            )

    def _execute_scoped_read(
        self,
        binding: ProjectScopeBinding,
        call: ToolCall,
    ) -> tuple[str, dict[str, str]]:
        return self._execute_bounded_read(
            binding,
            call,
            include_scope_metadata=True,
        )

    def _execute_workspace_read(
        self,
        call: ToolCall,
    ) -> tuple[str, dict[str, str]]:
        return self._execute_bounded_read(
            self._workspace_read_scope,
            call,
            include_scope_metadata=False,
        )

    def _execute_bounded_read(
        self,
        binding: ProjectScopeBinding,
        call: ToolCall,
        *,
        include_scope_metadata: bool,
    ) -> tuple[str, dict[str, str]]:
        raw_path = call.arguments.get("path", ".")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("Tool path must be a non-empty string")
        relative_parts, effective_path = self._scoped_path_parts(binding, raw_path)
        display_path = "/".join(relative_parts) or "."
        metadata = (
            {
                "project_scope": binding.relative_path,
                "project_scope_identity": _binding_identity_token(binding),
                "requested_path": raw_path,
                "effective_path": effective_path,
            }
            if include_scope_metadata
            else {}
        )
        with self._open_project_scope_fd(binding) as scope_fd:
            if call.name == "list_files":
                self._reject_unknown_arguments(call, {"path", "max_depth"})
                max_depth = call.arguments.get("max_depth", 3)
                if not isinstance(max_depth, int) or isinstance(max_depth, bool):
                    raise ValueError("max_depth must be an integer")
                if not 1 <= max_depth <= 6:
                    raise ValueError("max_depth must be between 1 and 6")
                output = self._list_files_scoped(
                    scope_fd,
                    binding,
                    raw_path,
                    relative_parts,
                    display_path,
                    max_depth,
                )
            elif call.name == "read_file":
                self._reject_unknown_arguments(call, {"path", "start_line", "end_line"})
                start_line = call.arguments.get("start_line", 1)
                end_line = call.arguments.get("end_line")
                if not isinstance(start_line, int) or isinstance(start_line, bool):
                    raise ValueError("start_line must be an integer")
                if end_line is not None and (
                    not isinstance(end_line, int) or isinstance(end_line, bool)
                ):
                    raise ValueError("end_line must be an integer")
                if start_line < 1 or (end_line is not None and end_line < 1):
                    raise ValueError("Line numbers must be positive")
                if end_line is not None and end_line - start_line + 1 > SCOPED_READ_MAX_LINES:
                    raise ValueError(
                        f"read_file can return at most {SCOPED_READ_MAX_LINES} lines"
                    )
                output = self._read_file_scoped(
                    scope_fd,
                    binding,
                    raw_path,
                    relative_parts,
                    start_line,
                    end_line,
                )
            else:
                self._reject_unknown_arguments(call, {"query", "path", "glob"})
                query = call.arguments.get("query")
                glob = call.arguments.get("glob")
                if not isinstance(query, str):
                    raise ValueError("query must be a string")
                if glob is not None and not isinstance(glob, str):
                    raise ValueError("glob must be a string")
                glob_pattern = _compile_search_glob(glob) if glob is not None else None
                output = self._search_text_scoped(
                    scope_fd,
                    binding,
                    raw_path,
                    relative_parts,
                    display_path,
                    query,
                    glob_pattern,
                )

            output = _bounded_tool_output(
                output,
                character_limit=max(0, self.settings.model_output_limit),
                byte_limit=max(0, self.settings.stored_output_limit),
            )

            # The descriptor above pins all actual reads to the selected directory. Rechecking
            # device/inode/ctime after the operation makes concurrent mutation or rename visible
            # and discards locally buffered output, including after rename-away/restore races.
            self._validated_project_scope(binding)
        return output, metadata

    @staticmethod
    def _reject_unknown_arguments(call: ToolCall, allowed: set[str]) -> None:
        unexpected = sorted(set(call.arguments) - allowed)
        if unexpected:
            raise ValueError(
                f"Unexpected arguments for {call.name}: {', '.join(unexpected)}"
            )

    @staticmethod
    def _scoped_path_parts(
        binding: ProjectScopeBinding,
        raw_path: str,
    ) -> tuple[tuple[str, ...], str]:
        relative_parts, effective_path = ToolRegistry._scoped_relative_path(
            binding,
            raw_path,
        )
        parts = list(PurePosixPath(raw_path).parts)
        if any(_is_git_path_component(part) for part in parts):
            raise WorkspaceViolation("Reading .git path components is not allowed")
        if _contains_secret_path_component(raw_path):
            raise WorkspaceViolation(
                "Secret-bearing environment paths (.env-family path components) "
                "cannot be read by the agent"
            )
        return relative_parts, effective_path

    @staticmethod
    def _scoped_relative_path(
        binding: ProjectScopeBinding,
        raw_path: str,
    ) -> tuple[tuple[str, ...], str]:
        if "\x00" in raw_path:
            raise WorkspaceViolation("Path must not contain a null byte")
        supplied = PurePosixPath(raw_path)
        if supplied.is_absolute():
            raise WorkspaceViolation("Absolute paths are not allowed")
        parts = list(supplied.parts)
        if any(part == ".." for part in parts):
            raise WorkspaceViolation(
                f"Path escapes selected project scope '{binding.relative_path}': {raw_path}"
            )
        relative_parts = tuple(part for part in parts if part not in {"", "."})
        effective_parts = (
            relative_parts
            if binding.relative_path == "."
            else (binding.relative_path, *relative_parts)
        )
        effective_path = "/".join(effective_parts) or "."
        return relative_parts, effective_path

    @staticmethod
    def _effective_scoped_path(
        binding: ProjectScopeBinding,
        raw_path: str,
    ) -> str:
        if not raw_path:
            raise WorkspaceViolation("Path must be a non-empty relative path")
        _parts, effective_path = ToolRegistry._scoped_relative_path(binding, raw_path)
        return effective_path

    @contextmanager
    def _open_project_scope_fd(
        self,
        binding: ProjectScopeBinding,
    ) -> Iterator[int]:
        lexical = self.workspace.root / binding.relative_path
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(lexical, flags)
        except OSError as exc:
            raise WorkspaceViolation(
                f"Selected project scope '{binding.relative_path}' is no longer available"
            ) from exc
        try:
            identity = os.fstat(descriptor)
            if not stat.S_ISDIR(identity.st_mode) or not _binding_matches_identity(
                binding,
                identity,
            ):
                raise WorkspaceViolation(
                    f"Selected project scope '{binding.relative_path}' was replaced after selection"
                )
            yield descriptor
        finally:
            os.close(descriptor)

    def _open_scoped_directory(
        self,
        scope_fd: int,
        binding: ProjectScopeBinding,
        raw_path: str,
        parts: tuple[str, ...],
    ) -> int:
        descriptor = os.dup(scope_fd)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            for part in parts:
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            return descriptor
        except OSError as exc:
            os.close(descriptor)
            raise WorkspaceViolation(
                "Path is unavailable or unsafe inside selected project scope "
                f"'{binding.relative_path}': {raw_path}"
            ) from exc

    @staticmethod
    def _open_relative_directory(
        base_fd: int,
        parts: tuple[str, ...],
    ) -> int:
        descriptor = os.dup(base_fd)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            for part in parts:
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            return descriptor
        except OSError:
            os.close(descriptor)
            raise

    def _open_scoped_node(
        self,
        scope_fd: int,
        binding: ProjectScopeBinding,
        raw_path: str,
        parts: tuple[str, ...],
    ) -> tuple[int, os.stat_result]:
        if not parts:
            descriptor = os.dup(scope_fd)
            return descriptor, os.fstat(descriptor)
        parent = self._open_scoped_directory(scope_fd, binding, raw_path, parts[:-1])
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(parts[-1], flags, dir_fd=parent)
        except OSError as exc:
            raise WorkspaceViolation(
                "Path is unavailable or unsafe inside selected project scope "
                f"'{binding.relative_path}': {raw_path}"
            ) from exc
        finally:
            os.close(parent)
        try:
            return descriptor, os.fstat(descriptor)
        except OSError:
            os.close(descriptor)
            raise

    def _validated_project_scope(self, binding: ProjectScopeBinding) -> Path:
        lexical = self.workspace.root / binding.relative_path
        try:
            identity = lexical.stat(follow_symlinks=False)
        except OSError as exc:
            raise WorkspaceViolation(
                f"Selected project scope '{binding.relative_path}' is no longer available"
            ) from exc
        if lexical.is_symlink() or not lexical.is_dir():
            raise WorkspaceViolation(
                f"Selected project scope '{binding.relative_path}' changed after selection"
            )
        if not _binding_matches_identity(binding, identity):
            raise WorkspaceViolation(
                f"Selected project scope '{binding.relative_path}' was replaced after selection"
            )
        resolved = lexical.resolve(strict=True)
        if resolved != binding.resolved_path:
            raise WorkspaceViolation(
                f"Selected project scope '{binding.relative_path}' changed after selection"
            )
        return resolved

    def _refresh_project_scope_binding(
        self,
        run_id: str,
        binding: ProjectScopeBinding,
    ) -> ProjectScopeBinding:
        """Refresh a scope generation after a controlled mutation without accepting a swap."""

        if self._project_scopes.get(run_id) is not binding:
            raise WorkspaceViolation("Selected project scope changed while a tool was running")
        lexical = self.workspace.root / binding.relative_path
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(lexical, flags)
        except OSError as exc:
            raise WorkspaceViolation(
                f"Selected project scope '{binding.relative_path}' is no longer available"
            ) from exc
        try:
            opened_identity = os.fstat(descriptor)
            if not stat.S_ISDIR(opened_identity.st_mode) or (
                opened_identity.st_dev,
                opened_identity.st_ino,
            ) != (binding.device, binding.inode):
                raise WorkspaceViolation(
                    f"Selected project scope '{binding.relative_path}' was replaced "
                    "while a tool was running"
                )
            try:
                path_identity = lexical.stat(follow_symlinks=False)
                resolved = lexical.resolve(strict=True)
                final_identity = lexical.stat(follow_symlinks=False)
            except OSError as exc:
                raise WorkspaceViolation(
                    f"Selected project scope '{binding.relative_path}' changed while a "
                    "tool was running"
                ) from exc
            if (
                path_identity.st_dev,
                path_identity.st_ino,
                path_identity.st_ctime_ns,
            ) != (
                opened_identity.st_dev,
                opened_identity.st_ino,
                opened_identity.st_ctime_ns,
            ) or (
                final_identity.st_dev,
                final_identity.st_ino,
                final_identity.st_ctime_ns,
            ) != (
                opened_identity.st_dev,
                opened_identity.st_ino,
                opened_identity.st_ctime_ns,
            ) or resolved != binding.resolved_path:
                raise WorkspaceViolation(
                    f"Selected project scope '{binding.relative_path}' changed while a "
                    "tool was running"
                )
            refreshed = ProjectScopeBinding(
                relative_path=binding.relative_path,
                resolved_path=binding.resolved_path,
                device=binding.device,
                inode=binding.inode,
                ctime_ns=opened_identity.st_ctime_ns,
            )
            if self._project_scopes.get(run_id) is not binding:
                raise WorkspaceViolation(
                    "Selected project scope changed while a tool was running"
                )
            self._project_scopes[run_id] = refreshed
            return refreshed
        finally:
            os.close(descriptor)

    @staticmethod
    def _project_scope_metadata(binding: ProjectScopeBinding) -> dict[str, str]:
        return {
            "project_scope": binding.relative_path,
            "project_scope_identity": _binding_identity_token(binding),
        }

    def _require_workspace_instruction_grant(self, run_id: str) -> None:
        try:
            run = self.workspace.storage.get_run(run_id)
        except KeyError as exc:
            raise ValueError(
                "Workspace mutation was blocked because the run is not persisted"
            ) from exc
        turn_index = run.turns[-1].index if run.turns else 1
        snapshot = self.workspace.storage.try_get_workspace_instruction_snapshot(
            run_id,
            turn_index,
        )
        granted = self._instruction_grants.get(run_id)
        if (
            snapshot is None
            or granted is None
            or not hmac.compare_digest(granted, snapshot.snapshot_sha256)
        ):
            raise ValueError(
                "Workspace mutation was blocked because this turn's immutable AGENTS.md "
                "snapshot is missing or not bound"
            )

    def _mutation_paths(
        self,
        call: ToolCall,
        *,
        binding: ProjectScopeBinding | None = None,
    ) -> list[str]:
        if call.name == "create_file":
            raw = call.arguments.get("path")
            if not isinstance(raw, str) or not raw.strip():
                raise PatchError("create_file path must be a non-empty string")
            return [
                self.workspace.relative(
                    self._resolve_mutation_path(
                        binding,
                        raw,
                        must_exist=False,
                    )
                )
            ]
        raw_patch = call.arguments.get("patch")
        if not isinstance(raw_patch, str):
            raise PatchError("apply_patch patch must be a string")
        paths: list[str] = []
        for file_patch in parse_unified_diff(raw_patch):
            path = self._resolve_mutation_path(binding, file_patch.path)
            if (
                file_patch.new_path is not None
                and file_patch.old_path is not None
                and file_patch.new_path != file_patch.old_path
            ):
                raise PatchError("File renames are not supported in v1")
            paths.append(self.workspace.relative(path))
        return sorted(set(paths))

    def _resolve_mutation_path(
        self,
        binding: ProjectScopeBinding | None,
        raw_path: str,
        *,
        must_exist: bool | None = None,
    ) -> Path:
        effective_path = (
            self._effective_scoped_path(binding, raw_path)
            if binding is not None
            else raw_path
        )
        return self.workspace.resolve_write(effective_path, must_exist=must_exist)

    def _mutation_baseline(
        self,
        call: ToolCall,
        *,
        binding: ProjectScopeBinding | None = None,
    ) -> dict[str, tuple[bool, str | None]]:
        baseline: dict[str, tuple[bool, str | None]] = {}
        for relative_path in self._mutation_paths(call, binding=binding):
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

    def _list_files_scoped(
        self,
        scope_fd: int,
        binding: ProjectScopeBinding,
        raw_path: str,
        parts: tuple[str, ...],
        display_path: str,
        max_depth: int,
    ) -> str:
        root_fd = self._open_scoped_directory(scope_fd, binding, raw_path, parts)
        rows: list[str] = []
        queue: deque[tuple[tuple[str, ...], int]] = deque([((), 0)])
        scanned_entries = 0
        truncated = False
        incomplete = False
        try:
            while queue and not truncated:
                relative_parts, depth = queue.popleft()
                try:
                    current_fd = self._open_relative_directory(root_fd, relative_parts)
                except OSError:
                    incomplete = True
                    continue
                try:
                    remaining_entries = SCOPED_LIST_MAX_ENTRIES - scanned_entries
                    if remaining_entries <= 0:
                        truncated = True
                        continue
                    names: list[str] = []
                    directory_capped = False
                    try:
                        with os.scandir(current_fd) as iterator:
                            for entry in iterator:
                                if len(names) >= remaining_entries:
                                    directory_capped = True
                                    break
                                names.append(entry.name)
                    except OSError:
                        incomplete = True
                        continue
                    directories: list[str] = []
                    files: list[str] = []
                    for name in sorted(names):
                        scanned_entries += 1
                        # A Git worktree commonly represents `.git` as a regular file rather
                        # than a directory. Apply the component boundary before branching on
                        # file type so listing cannot expose either representation.
                        if _is_git_path_component(name) or _is_secret_file(name):
                            continue
                        try:
                            item_status = os.stat(
                                name,
                                dir_fd=current_fd,
                                follow_symlinks=False,
                            )
                        except OSError:
                            incomplete = True
                            continue
                        if stat.S_ISDIR(item_status.st_mode) and not (
                            is_ignored_workspace_directory(name)
                        ):
                            directories.append(name)
                        elif stat.S_ISREG(item_status.st_mode) and not _is_secret_file(name):
                            files.append(name)

                    if depth < max_depth:
                        for name in directories:
                            child_parts = (*relative_parts, name)
                            rows.append(
                                f"{self._scoped_display_path(display_path, child_parts)}/"
                            )
                            queue.append((child_parts, depth + 1))
                            if len(rows) >= SCOPED_LIST_MAX_ROWS:
                                truncated = True
                                break
                    if not truncated:
                        for name in files:
                            rows.append(
                                self._scoped_display_path(
                                    display_path,
                                    (*relative_parts, name),
                                )
                            )
                            if len(rows) >= SCOPED_LIST_MAX_ROWS:
                                truncated = True
                                break
                    truncated = truncated or directory_capped
                finally:
                    os.close(current_fd)
        finally:
            os.close(root_fd)
        if truncated:
            rows.append("... output truncated")
        elif incomplete:
            rows.append("... listing incomplete")
        return "\n".join(rows) or "(empty directory)"

    def _read_file_scoped(
        self,
        scope_fd: int,
        binding: ProjectScopeBinding,
        raw_path: str,
        parts: tuple[str, ...],
        start_line: int,
        end_line: int | None,
    ) -> str:
        if end_line is not None and end_line < start_line:
            raise ValueError("end_line must not be before start_line")
        descriptor, file_status = self._open_scoped_node(
            scope_fd,
            binding,
            raw_path,
            parts,
        )
        if not stat.S_ISREG(file_status.st_mode):
            os.close(descriptor)
            raise ValueError(f"Not a file: {raw_path}")
        if not parts or _is_secret_file(parts[-1]):
            os.close(descriptor)
            raise ValueError("Secret-bearing environment files cannot be read by the agent")
        content = bytearray()
        file_truncated = file_status.st_size > SCOPED_READ_MAX_FILE_BYTES
        with _binary_stream_from_fd(descriptor) as stream:
            while len(content) < SCOPED_READ_MAX_FILE_BYTES:
                chunk = stream.read(
                    min(
                        SCOPED_READ_CHUNK_BYTES,
                        SCOPED_READ_MAX_FILE_BYTES - len(content),
                    )
                )
                if not chunk:
                    break
                content.extend(chunk)
            try:
                file_truncated = file_truncated or os.fstat(stream.fileno()).st_size > len(
                    content
                )
            except OSError:
                file_truncated = True
        raw_content = bytes(content)
        try:
            text = raw_content.decode("utf-8")
        except UnicodeDecodeError as exc:
            if not (
                file_truncated
                and exc.reason == "unexpected end of data"
                and exc.end == len(raw_content)
            ):
                raise
            raw_content = raw_content[: exc.start]
            text = raw_content.decode("utf-8")
            content = bytearray(raw_content)
        lines = text.splitlines()
        if file_truncated and content and not content.endswith((b"\n", b"\r")):
            lines = lines[:-1]
        if start_line > len(lines):
            if file_truncated:
                raise ValueError(
                    "Scoped file read limit was reached before the requested start_line"
                )
            raise ValueError("start_line is beyond the end of the file")
        requested_end = end_line if end_line is not None else start_line + 399
        selected = lines[start_line - 1 : requested_end]
        if any(len(line.encode("utf-8")) > SCOPED_READ_MAX_LINE_BYTES for line in selected):
            raise ValueError(
                f"A requested line exceeds the {SCOPED_READ_MAX_LINE_BYTES}-byte read limit"
            )
        output = "\n".join(
            f"{number:>6} | {line}" for number, line in enumerate(selected, start=start_line)
        )
        if file_truncated and requested_end > len(lines):
            output += f"\n... file read truncated at {SCOPED_READ_MAX_FILE_BYTES} bytes"
        return output

    def _search_text_scoped(
        self,
        scope_fd: int,
        binding: ProjectScopeBinding,
        raw_path: str,
        parts: tuple[str, ...],
        display_path: str,
        query: str,
        glob_pattern: SearchGlob | None,
    ) -> str:
        deadline = monotonic() + SEARCH_TOTAL_TIMEOUT_SECONDS
        expression = _compile_search_pattern(query)
        _remaining_search_time(deadline)
        target_fd, target_status = self._open_scoped_node(
            scope_fd,
            binding,
            raw_path,
            parts,
        )
        matches: list[str] = []
        rendered_length = 0
        scanned_bytes = 0
        scanned_files = 0
        scan_incomplete = False
        output_limit = max(0, self.settings.model_output_limit)

        def append_match(display_path: str, number: int, line: str) -> bool:
            """Append at most the remaining output budget; return true when it is full."""
            nonlocal rendered_length
            separator_length = 1 if matches else 0
            remaining = output_limit - rendered_length - separator_length
            if remaining <= 0:
                return True
            prefix = f"{display_path}:{number}:"
            if len(prefix) >= remaining:
                rendered = prefix[:remaining]
                was_truncated = len(prefix) > remaining or bool(line)
            else:
                line_budget = remaining - len(prefix)
                rendered = prefix + line[:line_budget]
                was_truncated = len(line) > line_budget
            matches.append(rendered)
            rendered_length += separator_length + len(rendered)
            return was_truncated

        def search_file(
            descriptor: int,
            file_status: os.stat_result,
            display_path: str,
        ) -> tuple[bool, bool, bool]:
            """Return (output_full, scan_incomplete, stop_scan), consuming the fd."""
            nonlocal scanned_bytes, scanned_files
            scanned_files += 1
            if file_status.st_size > SCOPED_SEARCH_MAX_FILE_BYTES:
                os.close(descriptor)
                return False, True, False
            if scanned_bytes >= SCOPED_SEARCH_MAX_TOTAL_BYTES:
                os.close(descriptor)
                return False, True, True

            file_bytes = 0
            try:
                with _binary_stream_from_fd(descriptor) as stream:
                    number = 0
                    pending = b""
                    while True:
                        _remaining_search_time(deadline)
                        file_remaining = SCOPED_SEARCH_MAX_FILE_BYTES - file_bytes
                        total_remaining = SCOPED_SEARCH_MAX_TOTAL_BYTES - scanned_bytes
                        if file_remaining <= 0 or total_remaining <= 0:
                            return False, True, total_remaining <= 0
                        read_limit = min(
                            SCOPED_SEARCH_MAX_LINE_BYTES,
                            file_remaining,
                            total_remaining,
                        )
                        chunk = stream.read(read_limit)
                        if not chunk:
                            if pending:
                                number += 1
                                try:
                                    line = pending.decode("utf-8").rstrip("\r")
                                except UnicodeDecodeError:
                                    return False, False, False
                                if _search_pattern_matches(
                                    expression,
                                    line,
                                    deadline=deadline,
                                ) and append_match(display_path, number, line):
                                    return True, False, True
                            break
                        file_bytes += len(chunk)
                        scanned_bytes += len(chunk)
                        buffer = pending + chunk
                        cursor = 0
                        while (newline := buffer.find(b"\n", cursor)) >= 0:
                            raw_line = buffer[cursor : newline + 1]
                            if len(raw_line) > SCOPED_SEARCH_MAX_LINE_BYTES:
                                return False, True, False
                            number += 1
                            try:
                                line = raw_line.decode("utf-8").rstrip("\r\n")
                            except UnicodeDecodeError:
                                # Binary and non-UTF-8 files are not searchable text evidence.
                                return False, False, False
                            if _search_pattern_matches(
                                expression,
                                line,
                                deadline=deadline,
                            ) and append_match(display_path, number, line):
                                return True, False, True
                            cursor = newline + 1
                        pending = buffer[cursor:]
                        if len(pending) > SCOPED_SEARCH_MAX_LINE_BYTES:
                            return False, True, False
                        try:
                            at_eof = os.fstat(stream.fileno()).st_size <= file_bytes
                        except OSError:
                            return False, True, False
                        if at_eof:
                            if pending:
                                number += 1
                                try:
                                    line = pending.decode("utf-8").rstrip("\r")
                                except UnicodeDecodeError:
                                    return False, False, False
                                if _search_pattern_matches(
                                    expression,
                                    line,
                                    deadline=deadline,
                                ) and append_match(display_path, number, line):
                                    return True, False, True
                            break
            except OSError:
                return False, True, False
            return False, False, False

        if stat.S_ISREG(target_status.st_mode):
            if (
                not parts
                or _is_secret_file(parts[-1])
                or (glob_pattern is not None and not glob_pattern.matches(parts))
            ):
                os.close(target_fd)
                return "No matches"
            output_full, scan_incomplete, _stop_scan = search_file(
                target_fd,
                target_status,
                display_path,
            )
            _remaining_search_time(deadline)
            return self._render_scoped_search_matches(
                matches,
                output_full or scan_incomplete,
                output_limit,
            )
        if not stat.S_ISDIR(target_status.st_mode):
            os.close(target_fd)
            raise ValueError(f"Not a file or directory: {raw_path}")

        queue: deque[tuple[str, ...]] = deque([()])
        file_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        output_full = False
        stop_scan = False
        scanned_entries = 0

        try:
            while queue and not output_full and not stop_scan:
                _remaining_search_time(deadline)
                relative_parts = queue.popleft()
                try:
                    current_fd = self._open_relative_directory(target_fd, relative_parts)
                except OSError:
                    scan_incomplete = True
                    continue
                try:
                    remaining_entries = SCOPED_SEARCH_MAX_ENTRIES - scanned_entries
                    if remaining_entries <= 0:
                        scan_incomplete = True
                        stop_scan = True
                        continue
                    names: list[str] = []
                    directory_capped = False
                    try:
                        with os.scandir(current_fd) as iterator:
                            for entry in iterator:
                                _remaining_search_time(deadline)
                                if len(names) >= remaining_entries:
                                    directory_capped = True
                                    break
                                names.append(entry.name)
                    except OSError:
                        scan_incomplete = True
                        continue
                    for name in sorted(names):
                        _remaining_search_time(deadline)
                        scanned_entries += 1
                        # Do not rely on the directory-only ignore branch below: linked
                        # worktrees use a regular `.git` file containing an external gitdir.
                        if _is_git_path_component(name) or _is_secret_file(name):
                            continue
                        try:
                            item_status = os.stat(
                                name,
                                dir_fd=current_fd,
                                follow_symlinks=False,
                            )
                        except OSError:
                            scan_incomplete = True
                            continue
                        item_parts = (*relative_parts, name)
                        if stat.S_ISDIR(item_status.st_mode):
                            if is_ignored_workspace_directory(name):
                                continue
                            queue.append(item_parts)
                            continue
                        if (
                            not stat.S_ISREG(item_status.st_mode)
                            or _is_secret_file(name)
                            or (
                                glob_pattern is not None
                                and not glob_pattern.matches(item_parts)
                            )
                        ):
                            continue
                        if scanned_files >= SCOPED_SEARCH_MAX_FILES:
                            scan_incomplete = True
                            stop_scan = True
                            break
                        file_fd: int | None = None
                        try:
                            file_fd = os.open(name, file_flags, dir_fd=current_fd)
                            opened_status = os.fstat(file_fd)
                            if not stat.S_ISREG(opened_status.st_mode):
                                os.close(file_fd)
                                file_fd = None
                                continue
                        except OSError:
                            if file_fd is not None:
                                os.close(file_fd)
                            scan_incomplete = True
                            continue
                        assert file_fd is not None
                        file_output_full, file_incomplete, file_stop_scan = search_file(
                            file_fd,
                            opened_status,
                            self._scoped_display_path(display_path, item_parts),
                        )
                        scan_incomplete = scan_incomplete or file_incomplete
                        output_full = output_full or file_output_full
                        stop_scan = stop_scan or file_stop_scan
                        if output_full or stop_scan:
                            break
                    if directory_capped:
                        scan_incomplete = True
                        stop_scan = True
                finally:
                    os.close(current_fd)
        finally:
            os.close(target_fd)
        _remaining_search_time(deadline)
        return self._render_scoped_search_matches(
            matches,
            output_full or scan_incomplete,
            output_limit,
        )

    @staticmethod
    def _scoped_display_path(base: str, parts: tuple[str, ...]) -> str:
        prefix = () if base == "." else tuple(PurePosixPath(base).parts)
        return "/".join((*prefix, *parts)) or "."

    @staticmethod
    def _render_scoped_search_matches(
        matches: list[str],
        truncated: bool,
        output_limit: int,
    ) -> str:
        output = "\n".join(matches)
        if truncated:
            marker = "... output truncated"
            separator = "\n" if output else ""
            reserved = len(separator) + len(marker)
            if reserved >= output_limit:
                return marker[:output_limit]
            output = output[: output_limit - reserved] + separator + marker
        return output[:output_limit] or "No matches"[:output_limit]

    def _apply_patch(
        self,
        run_id: str,
        patch: str,
        *,
        binding: ProjectScopeBinding | None = None,
    ) -> str:
        patches = parse_unified_diff(patch)
        planned: list[tuple[FilePatch, Path, str | None]] = []
        for file_patch in patches:
            if file_patch.old_path is None:
                path = self._resolve_mutation_path(
                    binding,
                    file_patch.path,
                    must_exist=False,
                )
                original = ""
            else:
                path = self._resolve_mutation_path(
                    binding,
                    file_patch.old_path,
                    must_exist=True,
                )
                original = path.read_text(encoding="utf-8")
                self._reject_credential_text(original)
            if file_patch.new_path is None:
                rendered = None
            else:
                if file_patch.old_path and file_patch.new_path != file_patch.old_path:
                    raise PatchError("File renames are not supported in v1")
                rendered = apply_file_patch(original, file_patch)
                self._reject_credential_text(rendered)
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

    def _create_file(
        self,
        run_id: str,
        path: str,
        content: str,
        *,
        binding: ProjectScopeBinding | None = None,
    ) -> str:
        self._reject_credential_text(content)
        target = self._resolve_mutation_path(binding, path, must_exist=False)
        self.workspace.snapshot(run_id, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.workspace.record_agent_version(run_id, target)
        return self.workspace.diff(run_id)

    def _reject_credential_text(self, content: str) -> None:
        if contains_redactable_secret(content, api_key=self.settings.api_key):
            raise ValueError(
                "Native file mutation was rejected because its content contains "
                "credential-like data"
            )

    async def _run_command(
        self,
        run_id: str,
        call: ToolCall,
        argv: list[str],
        cwd: str = ".",
        timeout_seconds: int | None = None,
        output_callback: OutputCallback | None = None,
        sandbox_bypass: bool = False,
        binding: ProjectScopeBinding | None = None,
    ) -> ToolResult:
        if not argv or not all(isinstance(item, str) and item for item in argv):
            raise ValueError("argv must contain non-empty strings")
        command_root = (
            self._validated_project_scope(binding)
            if binding is not None
            else self.workspace.root
        )
        hard_denial = _hard_command_denial(argv, command_root)
        if hard_denial is not None:
            raise ValueError(hard_denial)
        command_temp = tempfile.TemporaryDirectory(prefix="traceforge-command-")
        # macOS exposes /var as a symlink to /private/var. Seatbelt matches canonical
        # paths, so pass the resolved directory to both the profile and child environment.
        command_temp_path = await asyncio.to_thread(Path(command_temp.name).resolve)
        sandbox_home = command_temp_path / "home"
        sandbox_tmp = command_temp_path / "tmp"
        sandbox_cache = command_temp_path / "cache"
        try:
            for directory in (sandbox_home, sandbox_tmp, sandbox_cache):
                directory.mkdir()
            environment = _command_environment(
                command_root,
                home=sandbox_home,
                temp=sandbox_tmp,
                cache=sandbox_cache,
                include_agent_runtime=self.settings.demo_mode,
            )
            executable = (
                shutil.which(argv[0], path=environment["PATH"])
                if not Path(argv[0]).is_absolute()
                else argv[0]
            )
            if executable is None:
                raise ValueError(f"Executable not found: {argv[0]}")
            self.sandbox.validate_executable_scope(
                executable,
                execution_root=command_root,
            )
            if not self.settings.demo_mode and _is_agent_private_executable(
                Path(executable)
            ):
                raise ValueError(
                    "Commands cannot use TraceForge's private runtime. Create a separate "
                    "workspace-local environment and run the project through it."
                )
            effective_cwd = (
                self._effective_scoped_path(binding, cwd)
                if binding is not None
                else cwd
            )
            command_cwd = self.workspace.resolve_read(effective_cwd)
            if not command_cwd.is_dir():
                raise ValueError(f"Command cwd is not a directory: {cwd}")
            if binding is not None and not command_cwd.is_relative_to(command_root):
                raise WorkspaceViolation(
                    f"Command cwd escapes selected project scope '{binding.relative_path}': "
                    f"{cwd}"
                )
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
                execution_root=command_root,
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
        timed_out = False
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
            timed_out = True
        finally:
            self._processes.pop(run_id, None)
            command_temp.cleanup()
        scope_metadata: dict[str, str] = {}
        if binding is not None:
            binding = self._refresh_project_scope_binding(run_id, binding)
            scope_metadata = self._project_scope_metadata(binding)
        output = b"".join(chunks).decode(errors="replace")
        if truncated:
            output += f"\n... output truncated at {self.settings.stored_output_limit} bytes ...\n"
        if timed_out:
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
                    "cwd": self.workspace.relative(command_cwd),
                    "sandbox": launch.metadata,
                    **scope_metadata,
                },
            )
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
                **scope_metadata,
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
    workspace: Path,
    *,
    home: Path,
    temp: Path,
    cache: Path,
    include_agent_runtime: bool = False,
) -> dict[str, str]:
    environment = scrubbed_environment()
    # A TraceForge process commonly runs from its own virtual environment. Do not make
    # project package managers treat that private runtime as the selected project env.
    environment.pop("VIRTUAL_ENV", None)
    environment.pop("VIRTUAL_ENV_PROMPT", None)
    environment.pop("UV_PROJECT_ENVIRONMENT", None)
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment.pop("__PYVENV_LAUNCHER__", None)
    runtime_dirs = [
        workspace / ".venv" / "bin",
        workspace / "venv" / "bin",
    ]
    private_runtime = _agent_private_runtime_directory()
    if (
        not include_agent_runtime
        and private_runtime is not None
        and _workspace_environment_reuses_agent_runtime(workspace, private_runtime)
    ):
        # `uv run` discovers `<project>/.venv` independently from PATH. When TraceForge is
        # asked to work on its own checkout, that directory is the Agent's private runtime.
        # Force uv onto a distinct, workspace-local prefix instead of letting it bypass the
        # executable and PATH checks below.
        environment["UV_PROJECT_ENVIRONMENT"] = str(
            (workspace / ".traceforge-uv-venv").resolve()
        )
    if include_agent_runtime and private_runtime is not None:
        runtime_dirs.append(private_runtime)
    base_runtime = _base_python_runtime_directory()
    if base_runtime is not None:
        runtime_dirs.append(base_runtime)
    existing = environment.get("PATH", "").split(os.pathsep)
    ordered = [
        str(path)
        for path in runtime_dirs
        if path.is_dir()
        and (
            include_agent_runtime
            or private_runtime is None
            or path.resolve() != private_runtime
        )
    ]
    ordered.extend(
        path
        for path in existing
        if path
        and path not in ordered
        and (
            include_agent_runtime
            or private_runtime is None
            or Path(path).resolve() != private_runtime
        )
    )
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
            "PYTHONNOUSERSITE": "1",
            "PIP_REQUIRE_VIRTUALENV": "1",
        }
    )
    return environment


def _agent_private_runtime_directory() -> Path | None:
    return (
        Path(sys.executable).parent.resolve()
        if sys.prefix != sys.base_prefix
        else None
    )


def _base_python_runtime_directory() -> Path | None:
    base_executable = getattr(sys, "_base_executable", None)
    if isinstance(base_executable, str) and base_executable:
        directory = Path(base_executable).parent
        if directory.is_dir():
            return directory.resolve()
    directory = Path(sys.base_prefix) / "bin"
    return directory.resolve() if directory.is_dir() else None


def _workspace_environment_reuses_agent_runtime(
    workspace: Path, private_runtime: Path
) -> bool:
    for environment_name in (".venv", "venv"):
        candidate = workspace / environment_name / "bin"
        if candidate.is_dir() and candidate.resolve() == private_runtime:
            return True
    return False


def _is_agent_private_executable(executable: Path) -> bool:
    private_runtime = _agent_private_runtime_directory()
    if private_runtime is None:
        return False
    if _path_references_private_runtime(executable, private_runtime):
        return True
    try:
        with executable.open("rb") as stream:
            first_line = stream.readline(4_097)
    except OSError:
        return False
    if len(first_line) > 4_096 or not first_line.startswith(b"#!"):
        return False
    try:
        shebang = first_line[2:].decode("utf-8", errors="strict").strip()
        tokens = shlex.split(shebang)
    except (UnicodeDecodeError, ValueError):
        return False
    return any(
        token.startswith(os.sep)
        and _path_references_private_runtime(Path(token), private_runtime)
        for token in tokens
    )


def _path_references_private_runtime(path: Path, private_runtime: Path) -> bool:
    """Detect both resolved targets and intermediate links into the Agent runtime."""

    candidate = path if path.is_absolute() else path.absolute()
    visited: set[Path] = set()
    try:
        for _ in range(40):
            normalized = candidate.absolute()
            if normalized in visited:
                break
            visited.add(normalized)
            if candidate.parent.resolve() == private_runtime:
                return True
            if not candidate.is_symlink():
                break
            target = Path(os.readlink(candidate))
            candidate = target if target.is_absolute() else candidate.parent / target
        return path.resolve().parent == private_runtime
    except OSError:
        return False


def _is_dangerous_git(argv: list[str]) -> bool:
    return len(argv) > 2 and Path(argv[0]).name == "git" and argv[1:3] == ["reset", "--hard"]


def _is_dangerous_remove(argv: list[str]) -> bool:
    if Path(argv[0]).name != "rm":
        return False
    flags = "".join(item for item in argv[1:] if item.startswith("-"))
    return "r" in flags and "f" in flags


def _is_secret_file(name: str) -> bool:
    folded = name.casefold()
    return folded == ".env" or (
        folded.startswith(".env.") and folded != ".env.example"
    )


def _contains_secret_path_component(path: str) -> bool:
    return any(_is_secret_file(part) for part in PurePosixPath(path).parts)


def _is_git_path_component(name: str) -> bool:
    return name.casefold() == ".git"


@contextmanager
def _binary_stream_from_fd(descriptor: int) -> Iterator[BinaryIO]:
    try:
        stream = cast(BinaryIO, os.fdopen(descriptor, "rb", buffering=0))
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    with stream:
        yield stream


def _bounded_tool_output(
    output: str,
    *,
    character_limit: int,
    byte_limit: int,
) -> str:
    encoded = output.encode("utf-8")
    if len(output) <= character_limit and len(encoded) <= byte_limit:
        return output
    marker = "... output truncated"
    marker_only = _utf8_prefix(marker, character_limit, byte_limit)
    separator = "\n"
    reserved_characters = len(separator) + len(marker)
    reserved_bytes = len((separator + marker).encode("utf-8"))
    if character_limit <= reserved_characters or byte_limit <= reserved_bytes:
        return marker_only
    prefix = _utf8_prefix(
        output,
        character_limit - reserved_characters,
        byte_limit - reserved_bytes,
    )
    return prefix + separator + marker if prefix else marker_only


def _utf8_prefix(text: str, character_limit: int, byte_limit: int) -> str:
    if character_limit <= 0 or byte_limit <= 0:
        return ""
    candidate = text[:character_limit]
    encoded = candidate.encode("utf-8")
    if len(encoded) <= byte_limit:
        return candidate
    return encoded[:byte_limit].decode("utf-8", errors="ignore")


def _compile_search_glob(pattern: str) -> SearchGlob:
    if len(pattern) > SEARCH_MAX_GLOB_CHARS:
        raise ValueError(f"Glob must be at most {SEARCH_MAX_GLOB_CHARS} characters")
    parsed = PurePosixPath(pattern)
    if parsed.is_absolute():
        raise ValueError("Glob must be relative to the search root")
    if not parsed.parts:
        raise ValueError("Glob must not be empty")
    # PurePosixPath relative globs are right-aligned against candidate path
    # components. Compile every component once so scans do no repeated glob parsing
    # or regular-expression compilation.
    return SearchGlob(
        components=tuple(re.compile(fnmatch.translate(part)) for part in parsed.parts)
    )


def _compile_search_pattern(query: str) -> regex.Pattern[str]:
    if len(query) > SEARCH_MAX_QUERY_CHARS:
        raise ValueError(
            f"Regular expression must be at most {SEARCH_MAX_QUERY_CHARS} characters"
        )
    try:
        return regex.compile(query)
    except regex.error as exc:
        raise ValueError(f"Invalid regular expression: {exc}") from exc


def _remaining_search_time(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise ValueError("Text search exceeded its overall time limit")
    return remaining


def _search_pattern_matches(
    expression: regex.Pattern[str],
    line: str,
    *,
    deadline: float,
) -> bool:
    remaining = _remaining_search_time(deadline)
    timeout = min(SEARCH_REGEX_TIMEOUT_SECONDS, remaining)
    overall_deadline_is_tighter = remaining <= SEARCH_REGEX_TIMEOUT_SECONDS
    try:
        return expression.search(
            line,
            timeout=timeout,
            concurrent=True,
        ) is not None
    except TimeoutError as exc:
        if overall_deadline_is_tighter:
            raise ValueError("Text search exceeded its overall time limit") from exc
        raise ValueError("Regular expression search exceeded its time limit") from exc

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from traceforge.config import Settings
from traceforge.models import TaskPlan, ToolCall, ToolResult
from traceforge.patching import FilePatch, PatchError, apply_file_patch, parse_unified_diff
from traceforge.workspace import Workspace, WorkspaceViolation

OutputCallback = Callable[[str], Awaitable[None]]


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class PermissionAssessment:
    decision: PermissionDecision
    reason: str
    risk: Literal["unknown", "elevated", "dangerous"] = "unknown"


class ToolRegistry:
    def __init__(self, workspace: Workspace, settings: Settings) -> None:
        self.workspace = workspace
        self.settings = settings
        self._processes: dict[str, asyncio.subprocess.Process] = {}

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
                "Apply a standard unified diff transactionally inside the workspace.",
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
        if call.name != "run_command":
            return PermissionAssessment(PermissionDecision.ALLOW, "Workspace tool")
        argv = call.arguments.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) for item in argv)
        ):
            return PermissionAssessment(
                PermissionDecision.DENY, "argv must be a non-empty string list"
            )
        executable = Path(argv[0]).name
        if executable in {"sudo", "su", "shutdown", "reboot", "mkfs", "diskutil", "dd"}:
            return PermissionAssessment(
                PermissionDecision.DENY,
                f"{executable} is outside TraceForge's safety boundary",
                "dangerous",
            )
        if _is_dangerous_git(argv) or _is_dangerous_remove(argv):
            return PermissionAssessment(
                PermissionDecision.DENY,
                "Destructive version-control or recursive deletion command",
                "dangerous",
            )
        if _matches_accepted_check(argv, plan):
            return PermissionAssessment(
                PermissionDecision.ALLOW, "Command is an approved acceptance check"
            )
        if _is_read_only(argv):
            return PermissionAssessment(PermissionDecision.ALLOW, "Known read-only command")
        return PermissionAssessment(
            PermissionDecision.ASK,
            "The command can execute project code, modify files, access the network, "
            "or write externally",
            "elevated",
        )

    async def execute(
        self,
        run_id: str,
        call: ToolCall,
        *,
        output_callback: OutputCallback | None = None,
    ) -> ToolResult:
        try:
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
                    **call.arguments,
                )
            else:
                return ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    ok=False,
                    error=f"Unknown or non-executable tool: {call.name}",
                )
            return ToolResult(tool_call_id=call.id, name=call.name, ok=True, output=output)
        except (OSError, UnicodeError, ValueError, PatchError, WorkspaceViolation) as exc:
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                ok=False,
                error=str(exc),
            )

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
    ) -> ToolResult:
        if not argv or not all(isinstance(item, str) and item for item in argv):
            raise ValueError("argv must contain non-empty strings")
        executable = shutil.which(argv[0]) if not Path(argv[0]).is_absolute() else argv[0]
        if executable is None:
            raise ValueError(f"Executable not found: {argv[0]}")
        command_cwd = self.workspace.resolve_read(cwd)
        if not command_cwd.is_dir():
            raise ValueError(f"Command cwd is not a directory: {cwd}")
        timeout = timeout_seconds or self.settings.command_timeout
        if timeout > self.settings.max_command_timeout:
            raise ValueError(f"timeout_seconds exceeds {self.settings.max_command_timeout}")
        process = await asyncio.create_subprocess_exec(
            executable,
            *argv[1:],
            cwd=command_cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
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
                metadata={"timeout": True, "truncated": truncated, "argv": argv},
            )
        finally:
            self._processes.pop(run_id, None)
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


def _matches_accepted_check(argv: list[str], plan: TaskPlan | None) -> bool:
    if plan is None:
        return False
    return any(check.command == argv for check in plan.acceptance_checks if check.command)


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


def _is_dangerous_git(argv: list[str]) -> bool:
    return len(argv) > 2 and Path(argv[0]).name == "git" and argv[1:3] == ["reset", "--hard"]


def _is_dangerous_remove(argv: list[str]) -> bool:
    if Path(argv[0]).name != "rm":
        return False
    flags = "".join(item for item in argv[1:] if item.startswith("-"))
    return "r" in flags and "f" in flags


def _is_secret_file(name: str) -> bool:
    return name == ".env" or (name.startswith(".env.") and name != ".env.example")

from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

SandboxBackend = Literal["seatbelt", "bubblewrap", "none"]
SandboxExecution = Literal["enforced", "bypassed", "policy_only"]


@dataclass(frozen=True, slots=True)
class SandboxStatus:
    backend: SandboxBackend
    enforced: bool
    detail: str

    def as_dict(self) -> dict[str, str | bool]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SandboxLaunch:
    program: str
    arguments: list[str]
    metadata: dict[str, str | bool]


class CommandSandbox:
    """Build a fail-closed OS wrapper for commands that did not request an escape."""

    def __init__(
        self,
        workspace: Path,
        *,
        credential_file: Path | None = None,
        allow_network: bool = False,
    ) -> None:
        self.workspace = workspace.resolve(strict=True)
        self.credential_file = credential_file.resolve() if credential_file else None
        self.allow_network = allow_network
        self.status, self._program = _detect_backend(
            self.workspace, allow_network=allow_network
        )

    def prepare(
        self,
        executable: str,
        argv: list[str],
        *,
        cwd: Path,
        command_temp: Path,
        environment: dict[str, str],
        bypass: bool,
        execution_root: Path | None = None,
    ) -> SandboxLaunch:
        command_root = self._validated_execution_root(execution_root)
        self._validate_executable_scope(executable, command_root)
        resolved_cwd = cwd.resolve(strict=True)
        if not resolved_cwd.is_dir() or not resolved_cwd.is_relative_to(command_root):
            raise ValueError(
                f"Command cwd must stay inside the command root: {command_root}"
            )
        scoped = command_root != self.workspace
        if bypass and not scoped:
            return SandboxLaunch(
                executable,
                argv[1:],
                {
                    "status": "bypassed",
                    "backend": self.status.backend,
                    "enforced": False,
                    "detail": "The user approved this command outside the OS sandbox.",
                },
            )
        if not self.status.enforced or self._program is None:
            if scoped:
                raise ValueError(
                    "Commands in a selected project require an enforced OS sandbox"
                )
            return SandboxLaunch(
                executable,
                argv[1:],
                {
                    "status": "policy_only",
                    "backend": "none",
                    "enforced": False,
                    "detail": self.status.detail,
                },
            )
        if self.status.backend == "seatbelt":
            arguments = self._seatbelt_arguments(
                executable,
                argv,
                command_temp=command_temp,
                environment=environment,
                execution_root=command_root,
            )
        else:
            arguments = self._bubblewrap_arguments(
                executable,
                argv,
                cwd=resolved_cwd,
                command_temp=command_temp,
                execution_root=command_root,
            )
        return SandboxLaunch(
            self._program,
            arguments,
            {
                "status": "enforced",
                "backend": self.status.backend,
                "enforced": True,
                "network": self._network_mode(),
                "command_root": str(command_root),
                "scope_enforced": scoped,
                "bypass_requested": bypass,
                "detail": self.status.detail,
            },
        )

    def _network_mode(self) -> str:
        if self.allow_network:
            return "allowed"
        return (
            "loopback_only"
            if self.status.backend == "seatbelt"
            else "isolated_namespace"
        )

    def _validated_execution_root(self, requested: Path | None) -> Path:
        if requested is None:
            return self.workspace
        resolved = requested.resolve(strict=True)
        if not resolved.is_dir() or not resolved.is_relative_to(self.workspace):
            raise ValueError(
                "Command root must be a directory inside the configured workspace"
            )
        return resolved

    def validate_executable_scope(
        self,
        executable: str,
        *,
        execution_root: Path | None = None,
    ) -> None:
        """Reject absolute workspace executables that escape a selected project."""

        command_root = self._validated_execution_root(execution_root)
        self._validate_executable_scope(executable, command_root)

    def _validate_executable_scope(self, executable: str, command_root: Path) -> None:
        candidate = Path(executable)
        if command_root == self.workspace or not candidate.is_absolute():
            return
        lexical_path = Path(os.path.abspath(executable))
        try:
            resolved_path = lexical_path.resolve()
        except (OSError, RuntimeError):
            resolved_path = lexical_path
        for path in (lexical_path, resolved_path):
            if path.is_relative_to(self.workspace) and not path.is_relative_to(
                command_root
            ):
                raise ValueError(
                    "Absolute executable is outside the selected project root: "
                    f"{executable}"
                )

    def _seatbelt_arguments(
        self,
        executable: str,
        argv: list[str],
        *,
        command_temp: Path,
        environment: dict[str, str],
        execution_root: Path,
    ) -> list[str]:
        home = Path.home().resolve()
        executable_path = Path(executable)
        resolved_parent = executable_path.resolve().parent
        read_roots = {execution_root, resolved_parent}
        if resolved_parent.name == "bin":
            read_roots.add(resolved_parent.parent)
        # A virtual-environment interpreter is commonly a symlink into the base
        # Python installation. Python still reads pyvenv.cfg beside that symlink,
        # so permit the visible venv root as well as the resolved interpreter.
        visible_parent = executable_path.parent.resolve()
        read_roots.add(visible_parent)
        if visible_parent.name == "bin":
            read_roots.add(visible_parent.parent)
        for raw in environment.get("PATH", "").split(os.pathsep):
            if not raw:
                continue
            path = Path(raw).resolve()
            if path.exists() and path.is_relative_to(home):
                read_roots.add(path)
        read_roots = {path for path in read_roots if path.is_relative_to(home)}
        definitions: list[tuple[str, Path]] = [
            ("WORKSPACE", self.workspace),
            ("COMMAND_ROOT", execution_root),
            ("COMMAND_TEMP", command_temp),
            ("HOME_ROOT", home),
        ]
        read_exclusions = ["(require-not (subpath (param \"COMMAND_ROOT\")))"]
        for index, path in enumerate(sorted(read_roots)):
            key = f"READ_ROOT_{index}"
            definitions.append((key, path))
            read_exclusions.append(f'(require-not (subpath (param "{key}")))')

        protected_rules: list[str] = []
        for index, path in enumerate(self._sensitive_paths()):
            key = f"SENSITIVE_{index}"
            definitions.append((key, path))
            protected_rules.extend(
                [
                    f'(deny file-read* (literal (param "{key}")) (subpath (param "{key}")))',
                    f'(deny file-write* (literal (param "{key}")) (subpath (param "{key}")))',
                ]
            )
        for index, git_dir in enumerate(
            sorted({self.workspace / ".git", execution_root / ".git"})
        ):
            if not git_dir.exists():
                continue
            key = f"GIT_DIR_{index}"
            definitions.append((key, git_dir))
            protected_rules.append(
                f'(deny file-write* (literal (param "{key}")) '
                f'(subpath (param "{key}")))'
            )

        network_rules: list[str] = []
        if not self.allow_network:
            # Network containment is independent from the file, credential, and host
            # boundaries below. When the operator explicitly allows network, only these
            # outbound denials are dropped; every other rule still applies.
            network_rules = [
                (
                    "(deny network-outbound (require-all (socket-domain AF_INET) "
                    '(require-not (remote ip "localhost:*"))))'
                ),
                (
                    "(deny network-outbound (require-all (socket-domain AF_INET6) "
                    '(require-not (remote ip "localhost:*"))))'
                ),
            ]
        profile = "\n".join(
            [
                "(version 1)",
                "(allow default)",
                *network_rules,
                # Keep directory metadata visible so executables nested under the
                # user's home can be traversed, while denying file contents.
                "(deny file-read-data (require-all (subpath (param \"HOME_ROOT\")) ",
                *[f"  {item}" for item in read_exclusions],
                "))",
                (
                    "(deny file-read-data (require-all "
                    '(subpath (param "WORKSPACE")) '
                    '(require-not (subpath (param "COMMAND_ROOT")))))'
                ),
                (
                    "(deny file-write* (require-all "
                    '(require-not (subpath (param "COMMAND_ROOT"))) '
                    '(require-not (subpath (param "COMMAND_TEMP"))) '
                    '(require-not (literal "/dev/null"))))'
                ),
                (
                    '(deny file-write-unlink (require-all '
                    '(literal (param "COMMAND_ROOT")) '
                    "(vnode-type DIRECTORY)))"
                ),
                *protected_rules,
            ]
        )
        parameters = [f"-D{key}={value}" for key, value in definitions]
        return ["-p", profile, *parameters, "--", executable, *argv[1:]]

    def _bubblewrap_arguments(
        self,
        executable: str,
        argv: list[str],
        *,
        cwd: Path,
        command_temp: Path,
        execution_root: Path,
    ) -> list[str]:
        arguments = [
            "--die-with-parent",
            "--new-session",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
        ]
        if not self.allow_network:
            # A separate network namespace is the only network containment here. When the
            # operator explicitly allows network, keep every other namespace and file
            # boundary but let the command share the host network stack.
            arguments.append("--unshare-net")
        arguments.extend(
            [
                "--ro-bind",
                "/",
                "/",
                "--dev",
                "/dev",
                "--proc",
                "/proc",
            ]
        )
        if execution_root != self.workspace:
            # The initial read-only root still exposes the host workspace. Mask it before
            # mounting the selected project back at its original absolute path so sibling
            # project contents are absent from the command's namespace.
            arguments.extend(["--tmpfs", str(self.workspace)])
        arguments.extend(
            [
                "--bind",
                str(execution_root),
                str(execution_root),
                "--bind",
                str(command_temp),
                str(command_temp),
            ]
        )
        for git_dir in [execution_root / ".git"]:
            if git_dir.exists():
                arguments.extend(["--ro-bind", str(git_dir), str(git_dir)])
        for path in self._sensitive_paths():
            if path.is_dir():
                arguments.extend(["--tmpfs", str(path)])
            else:
                arguments.extend(["--ro-bind", "/dev/null", str(path)])
        arguments.extend(["--chdir", str(cwd), "--", executable, *argv[1:]])
        return arguments

    def _sensitive_paths(self) -> list[Path]:
        home = Path.home()
        candidates = [
            home / ".ssh",
            home / ".aws",
            home / ".gnupg",
            home / ".kube",
            home / ".netrc",
            home / ".docker" / "config.json",
            home / ".config" / "gcloud",
        ]
        if self.credential_file:
            candidates.append(self.credential_file)
        candidates.extend(
            path
            for path in self.workspace.rglob(".env*")
            if path.name != ".env.example"
        )
        protected: set[Path] = set()
        for candidate in candidates[:200]:
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            protected.add(resolved)
        return sorted(protected)


def sandbox_status(workspace: Path, *, allow_network: bool = False) -> SandboxStatus:
    return _detect_backend(
        workspace.resolve(strict=True), allow_network=allow_network
    )[0]


def _detect_backend(
    workspace: Path, *, allow_network: bool = False
) -> tuple[SandboxStatus, str | None]:
    system = platform.system()
    if system == "Darwin":
        executable = Path("/usr/bin/sandbox-exec")
        if executable.is_file() and _seatbelt_probe(str(executable)):
            network_detail = (
                "with authorized outbound network"
                if allow_network
                else "with loopback-only network"
            )
            return (
                SandboxStatus(
                    backend="seatbelt",
                    enforced=True,
                    detail=(
                        "Seatbelt limits writes to the workspace and isolated command temp, "
                        f"{network_detail}."
                    ),
                ),
                str(executable),
            )
        return (
            SandboxStatus(
                backend="none",
                enforced=False,
                detail="Seatbelt is unavailable; command safety is policy-only.",
            ),
            None,
        )
    if system == "Linux":
        raw = shutil.which("bwrap")
        if raw:
            executable = Path(raw).resolve()
            if executable.is_relative_to(workspace):
                raw = None
            elif executable.stat().st_mode & stat.S_ISUID:
                return (
                    SandboxStatus(
                        backend="none",
                        enforced=False,
                        detail="Setuid bubblewrap is rejected; install a current non-setuid build.",
                    ),
                    None,
                )
            elif _bubblewrap_probe(str(executable)):
                network_detail = (
                    "shared host network"
                    if allow_network
                    else "isolated network"
                )
                return (
                    SandboxStatus(
                        backend="bubblewrap",
                        enforced=True,
                        detail=(
                            "Bubblewrap limits writes to the workspace and isolated command "
                            f"temp, with isolated processes and {network_detail}."
                        ),
                    ),
                    str(executable),
                )
        return (
            SandboxStatus(
                backend="none",
                enforced=False,
                detail=(
                    "Install working bubblewrap for OS enforcement; "
                    "command safety is policy-only."
                ),
            ),
            None,
        )
    return (
        SandboxStatus(
            backend="none",
            enforced=False,
            detail=(
                "This operating system has no TraceForge sandbox backend; "
                "safety is policy-only."
            ),
        ),
        None,
    )


@lru_cache(maxsize=1)
def _seatbelt_probe(executable: str) -> bool:
    try:
        result = subprocess.run(
            [executable, "-p", "(version 1)\n(allow default)", "/usr/bin/true"],
            capture_output=True,
            check=False,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


@lru_cache(maxsize=8)
def _bubblewrap_probe(executable: str) -> bool:
    try:
        result = subprocess.run(
            [
                executable,
                "--die-with-parent",
                "--new-session",
                "--unshare-user",
                "--unshare-pid",
                "--unshare-ipc",
                "--unshare-uts",
                "--unshare-net",
                "--ro-bind",
                "/",
                "/",
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "/bin/true",
            ],
            capture_output=True,
            check=False,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0

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

    def __init__(self, workspace: Path, *, credential_file: Path | None = None) -> None:
        self.workspace = workspace.resolve(strict=True)
        self.credential_file = credential_file.resolve() if credential_file else None
        self.status, self._program = _detect_backend(self.workspace)

    def prepare(
        self,
        executable: str,
        argv: list[str],
        *,
        cwd: Path,
        command_temp: Path,
        environment: dict[str, str],
        bypass: bool,
    ) -> SandboxLaunch:
        if bypass:
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
                executable, argv, command_temp=command_temp, environment=environment
            )
        else:
            arguments = self._bubblewrap_arguments(
                executable, argv, cwd=cwd, command_temp=command_temp
            )
        return SandboxLaunch(
            self._program,
            arguments,
            {
                "status": "enforced",
                "backend": self.status.backend,
                "enforced": True,
                "network": (
                    "loopback_only"
                    if self.status.backend == "seatbelt"
                    else "isolated_namespace"
                ),
                "detail": self.status.detail,
            },
        )

    def _seatbelt_arguments(
        self,
        executable: str,
        argv: list[str],
        *,
        command_temp: Path,
        environment: dict[str, str],
    ) -> list[str]:
        home = Path.home().resolve()
        executable_path = Path(executable)
        resolved_parent = executable_path.resolve().parent
        read_roots = {self.workspace, resolved_parent}
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
            ("COMMAND_TEMP", command_temp),
            ("HOME_ROOT", home),
        ]
        read_exclusions = ["(require-not (subpath (param \"WORKSPACE\")))"]
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
        git_dir = self.workspace / ".git"
        if git_dir.exists():
            definitions.append(("GIT_DIR", git_dir))
            protected_rules.append(
                '(deny file-write* (literal (param "GIT_DIR")) (subpath (param "GIT_DIR")))'
            )

        profile = "\n".join(
            [
                "(version 1)",
                "(allow default)",
                (
                    "(deny network-outbound (require-all (socket-domain AF_INET) "
                    '(require-not (remote ip "localhost:*"))))'
                ),
                (
                    "(deny network-outbound (require-all (socket-domain AF_INET6) "
                    '(require-not (remote ip "localhost:*"))))'
                ),
                # Keep directory metadata visible so executables nested under the
                # user's home can be traversed, while denying file contents.
                "(deny file-read-data (require-all (subpath (param \"HOME_ROOT\")) ",
                *[f"  {item}" for item in read_exclusions],
                "))",
                (
                    "(deny file-write* (require-all "
                    '(require-not (subpath (param "WORKSPACE"))) '
                    '(require-not (subpath (param "COMMAND_TEMP"))) '
                    '(require-not (literal "/dev/null"))))'
                ),
                (
                    '(deny file-write-unlink (require-all (literal (param "WORKSPACE")) '
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
    ) -> list[str]:
        arguments = [
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
            "--bind",
            str(self.workspace),
            str(self.workspace),
            "--bind",
            str(command_temp),
            str(command_temp),
        ]
        git_dir = self.workspace / ".git"
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


def sandbox_status(workspace: Path) -> SandboxStatus:
    return _detect_backend(workspace.resolve(strict=True))[0]


def _detect_backend(workspace: Path) -> tuple[SandboxStatus, str | None]:
    system = platform.system()
    if system == "Darwin":
        executable = Path("/usr/bin/sandbox-exec")
        if executable.is_file() and _seatbelt_probe(str(executable)):
            return (
                SandboxStatus(
                    backend="seatbelt",
                    enforced=True,
                    detail=(
                        "Seatbelt limits writes to the workspace and isolated command temp, "
                        "with loopback-only network."
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
                return (
                    SandboxStatus(
                        backend="bubblewrap",
                        enforced=True,
                        detail=(
                            "Bubblewrap limits writes to the workspace and isolated command "
                            "temp, with isolated processes and network."
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

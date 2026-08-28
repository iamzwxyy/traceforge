from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import http.client
import json
import os
import platform
import secrets
import shlex
import shutil
import socket
import sqlite3
import stat
import time
import webbrowser
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from threading import Thread
from typing import Annotated, Any
from urllib.parse import urlsplit

import typer

from traceforge import __version__

app = typer.Typer(
    name="traceforge",
    help="A local coding agent that proves its work. Run without a command to launch the UI.",
    no_args_is_help=False,
    invoke_without_command=True,
)

_INSTANCE_LOCK_NAME = "traceforge-instance.lock"
_INSTANCE_RECORD_LIMIT = 4 * 1024
_INSTANCE_STARTUP_WAIT_SECONDS = 5.0
_INSTANCE_RETRY_INTERVAL_SECONDS = 0.1
_HEALTH_RESPONSE_LIMIT = 8 * 1024


@dataclass(frozen=True, slots=True)
class _InstanceRecord:
    pid: int
    url: str
    version: str
    instance_id: str | None
    config_fingerprint: str | None


@dataclass(slots=True)
class _InstanceLock:
    descriptor: int
    path: Path

    def publish(
        self, url: str, *, instance_id: str, config_fingerprint: str
    ) -> None:
        payload = json.dumps(
            {
                "config_fingerprint": config_fingerprint,
                "instance_id": instance_id,
                "pid": os.getpid(),
                "url": url,
                "version": __version__,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(payload) > _INSTANCE_RECORD_LIMIT:
            raise ValueError("TraceForge instance metadata is unexpectedly large")
        os.lseek(self.descriptor, 0, os.SEEK_SET)
        os.ftruncate(self.descriptor, 0)
        os.write(self.descriptor, payload)
        os.fsync(self.descriptor)

    def close(self) -> None:
        if self.descriptor < 0:
            return
        descriptor = self.descriptor
        self.descriptor = -1
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


class _InstanceBusy(Exception):
    def __init__(self, record: _InstanceRecord | None) -> None:
        super().__init__("another TraceForge process holds the instance lock")
        self.record = record


def _read_instance_record(descriptor: int) -> _InstanceRecord | None:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        raw_record = os.read(descriptor, _INSTANCE_RECORD_LIMIT + 1)
        if not raw_record or len(raw_record) > _INSTANCE_RECORD_LIMIT:
            return None
        payload = json.loads(raw_record)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    pid = payload.get("pid")
    url = payload.get("url")
    version = payload.get("version")
    instance_id = payload.get("instance_id")
    config_fingerprint = payload.get("config_fingerprint")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(url, str)
        or len(url) > 2_048
        or _health_target(url) is None
        or not isinstance(version, str)
        or not _valid_optional_instance_identity(instance_id, config_fingerprint)
    ):
        return None
    return _InstanceRecord(
        pid=pid,
        url=url,
        version=version,
        instance_id=instance_id,
        config_fingerprint=config_fingerprint,
    )


def _valid_optional_instance_identity(
    instance_id: object, config_fingerprint: object
) -> bool:
    if instance_id is None and config_fingerprint is None:
        return True
    return bool(
        isinstance(instance_id, str)
        and 16 <= len(instance_id) <= 128
        and all(character.isalnum() or character in "-_" for character in instance_id)
        and isinstance(config_fingerprint, str)
        and len(config_fingerprint) == 64
        and all(character in "0123456789abcdef" for character in config_fingerprint)
    )


def _acquire_instance_lock(data_dir: Path) -> _InstanceLock:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / _INSTANCE_LOCK_NAME
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError(errno.EINVAL, "instance lock is not a regular file", path)
        if file_stat.st_uid != os.getuid() or file_stat.st_nlink != 1:
            raise OSError(errno.EPERM, "instance lock has unsafe ownership or links", path)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            record = _read_instance_record(descriptor)
            raise _InstanceBusy(record) from exc
        os.fchmod(descriptor, 0o600)
        # A new owner must not expose a stale healthy URL while it is still starting.
        os.ftruncate(descriptor, 0)
        return _InstanceLock(descriptor=descriptor, path=path)
    except BaseException:
        os.close(descriptor)
        raise


def _reuse_existing_instance(
    record: _InstanceRecord | None,
    *,
    config_fingerprint: str,
    open_browser: bool,
) -> bool:
    if (
        record is None
        or record.version != __version__
        or record.instance_id is None
        or record.config_fingerprint != config_fingerprint
        or not _server_ready(
            record.url,
            instance_id=record.instance_id,
        )
    ):
        return False
    typer.echo(f"TraceForge is already running at {record.url}")
    if open_browser:
        webbrowser.open(record.url)
    return True


def _acquire_or_reuse_instance(
    data_dir: Path,
    *,
    config_fingerprint: str,
    open_browser: bool,
    wait_timeout: float,
) -> _InstanceLock | None:
    deadline = time.monotonic() + max(0.0, wait_timeout)
    while True:
        try:
            return _acquire_instance_lock(data_dir)
        except _InstanceBusy as exc:
            record = exc.record
            if _reuse_existing_instance(
                record,
                config_fingerprint=config_fingerprint,
                open_browser=open_browser,
            ):
                return None
            if (
                record is not None
                and (
                    record.version != __version__
                    or record.instance_id is None
                    or record.config_fingerprint != config_fingerprint
                )
            ):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(_INSTANCE_RETRY_INTERVAL_SECONDS, remaining))


def _instance_fingerprint(workspace: Path, *, host: str, port: int) -> str:
    payload = json.dumps(
        {
            "host": host.strip().lower(),
            "port": port,
            "workspace": str(workspace),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _reserve_listener(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(2_048)
        listener.set_inheritable(False)
    except BaseException:
        listener.close()
        raise
    return listener


def _suggested_port(host: str) -> int:
    with _reserve_listener(host, 0) as listener:
        address = listener.getsockname()
        return int(address[1])


def _retry_command(
    command: str,
    *,
    host: str,
    port: int,
    workspace: Path | None = None,
    open_browser: bool = True,
) -> str:
    arguments = ["uv", "run", "traceforge", command]
    if workspace is not None:
        arguments.extend(("--workspace", str(workspace)))
    if host != "127.0.0.1":
        arguments.extend(("--host", host))
    arguments.extend(("--port", str(port)))
    if command == "serve" and not open_browser:
        arguments.append("--no-open-browser")
    return shlex.join(arguments)


def _run_server(
    application: Any,
    listener: socket.socket,
    *,
    host: str,
    port: int,
) -> None:
    import uvicorn

    config = uvicorn.Config(application, host=host, port=port)
    uvicorn.Server(config).run(sockets=[listener])


@app.callback()
def launch(ctx: typer.Context) -> None:
    """Launch TraceForge, or choose a command for advanced workflows."""

    if ctx.invoked_subcommand is None:
        _serve_application(None, host="127.0.0.1", port=8765, open_browser=True)


def _writable_directory(directory: Path) -> tuple[bool, str]:
    try:
        with NamedTemporaryFile(prefix=".traceforge-doctor-", dir=directory):
            pass
    except OSError as exc:
        return False, str(exc)
    return True, str(directory)


def _available_port(host: str, port: int) -> tuple[bool, str]:
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        return False, str(exc)
    last_error = "No bindable address found"
    for family, socktype, protocol, _, address in addresses:
        try:
            with socket.socket(family, socktype, protocol) as candidate:
                candidate.bind(address)
        except OSError as exc:
            last_error = str(exc)
            continue
        return True, f"{host}:{port}"
    return False, last_error


def _doctor_line(status: str, label: str, detail: str) -> None:
    colors = {"PASS": typer.colors.GREEN, "WARN": typer.colors.YELLOW, "FAIL": typer.colors.RED}
    typer.secho(f"[{status}]", fg=colors[status], bold=True, nl=False)
    typer.echo(f" {label} · {detail}")


@app.command()
def doctor(
    workspace: Annotated[
        Path | None,
        typer.Option(
            exists=True,
            file_okay=False,
            resolve_path=True,
            help="Optional direct-task root to check instead of Documents/TraceForge.",
        ),
    ] = None,
    host: Annotated[str, typer.Option(help="Bind address to check.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8765,
    require_os_sandbox: Annotated[
        bool, typer.Option(help="Fail if Seatbelt or Bubblewrap is not enforced.")
    ] = False,
    probe_model: Annotated[
        bool, typer.Option(help="Make one real native-tool-call connection probe.")
    ] = False,
) -> None:
    """Check local readiness without printing credential values."""
    from traceforge.config import Settings
    from traceforge.events import EventBroker
    from traceforge.runtime import AgentRuntime, validate_credential_file
    from traceforge.sandbox import sandbox_status
    from traceforge.storage import Storage

    settings = Settings.from_env(workspace, require_api_key=False)
    failures = 0
    warnings = 0

    typer.echo("TraceForge doctor")
    supported = platform.system() in {"Darwin", "Linux"}
    _doctor_line("PASS" if supported else "FAIL", "Platform", platform.platform())
    failures += not supported

    write_targets = (
        ("Direct-task root write", settings.workspace),
        ("State write", settings.data_dir),
    )
    for label, directory in write_targets:
        ready, detail = _writable_directory(directory)
        _doctor_line("PASS" if ready else "FAIL", label, detail)
        failures += not ready

    static_index = resources.files("traceforge").joinpath("static", "index.html")
    bundle_ready = static_index.is_file()
    _doctor_line(
        "PASS" if bundle_ready else "FAIL",
        "Web bundle",
        "packaged UI found" if bundle_ready else "static/index.html is missing",
    )
    failures += not bundle_ready

    port_ready, port_detail = _available_port(host, port)
    _doctor_line("PASS" if port_ready else "FAIL", "Listen address", port_detail)
    failures += not port_ready

    sandbox = sandbox_status(settings.workspace)
    if sandbox.enforced:
        _doctor_line("PASS", "Command sandbox", f"{sandbox.backend} enforced")
    else:
        status = "FAIL" if require_os_sandbox else "WARN"
        _doctor_line(status, "Command sandbox", sandbox.detail)
        failures += require_os_sandbox
        warnings += not require_os_sandbox

    try:
        storage = Storage(settings.data_dir / "traceforge.db")
    except (OSError, sqlite3.Error) as exc:
        _doctor_line("FAIL", "State database", str(exc))
        failures += 1
        storage = None
    else:
        _doctor_line("PASS", "State database", "SQLite opened and migrations applied")

    runtime = AgentRuntime(settings, storage, EventBroker(storage)) if storage else None
    try:
        if runtime:
            config = runtime.provider_config
            if config.credential_file:
                try:
                    validate_credential_file(config.credential_file)
                except ValueError as exc:
                    credential_ready = False
                    credential_detail = str(exc)
                else:
                    credential_ready = runtime.credential_configured(config)
                    credential_detail = (
                        "owner-only credential file is readable"
                        if credential_ready
                        else "credential file must contain exactly one non-empty line"
                    )
            elif settings.api_key:
                credential_ready = True
                credential_detail = "OPENAI_API_KEY is set"
            else:
                credential_ready = False
                credential_detail = "enter an API key in Model settings"
            _doctor_line(
                "PASS" if credential_ready else "WARN",
                "Model credential",
                credential_detail,
            )
            warnings += not credential_ready
        else:
            credential_ready = False

        if probe_model:
            if not runtime:
                _doctor_line("FAIL", "Model probe", "state database is not ready")
                failures += 1
            elif not credential_ready:
                _doctor_line("FAIL", "Model probe", "credential is not ready")
                failures += 1
            else:
                try:
                    result = asyncio.run(runtime.test_connection())
                except ValueError as exc:
                    probe_ready = False
                    probe_detail = str(exc)
                else:
                    probe_ready = bool(result["ok"])
                    probe_detail = str(result["detail"])
                _doctor_line("PASS" if probe_ready else "FAIL", "Model probe", probe_detail)
                failures += not probe_ready
    finally:
        if runtime:
            asyncio.run(runtime.shutdown())
        if storage:
            storage.close()

    typer.echo()
    if failures:
        typer.secho(
            f"NOT READY · {failures} failure(s), {warnings} warning(s)",
            fg=typer.colors.RED,
            bold=True,
        )
        raise typer.Exit(code=1)
    typer.secho(
        f"READY TO SERVE · {warnings} warning(s)", fg=typer.colors.GREEN, bold=True
    )


@app.command()
def serve(
    workspace: Annotated[
        Path | None,
        typer.Option(
            exists=True,
            file_okay=False,
            resolve_path=True,
            help="Optional direct-task root override; defaults to Documents/TraceForge.",
        ),
    ] = None,
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8765,
    open_browser: Annotated[
        bool, typer.Option(help="Open the local UI after the server starts.")
    ] = True,
) -> None:
    """Run the local TraceForge web application."""

    _serve_application(workspace, host=host, port=port, open_browser=open_browser)


def _serve_application(
    workspace: Path | None,
    *,
    host: str,
    port: int,
    open_browser: bool,
    startup_wait: float | None = None,
) -> None:
    from traceforge.config import Settings

    settings = Settings.from_env(workspace, require_api_key=False)
    url = _browser_url(host, port)
    config_fingerprint = _instance_fingerprint(settings.workspace, host=host, port=port)
    instance_id = secrets.token_urlsafe(24)
    wait_timeout = (
        _INSTANCE_STARTUP_WAIT_SECONDS if startup_wait is None else startup_wait
    )
    try:
        instance_lock = _acquire_or_reuse_instance(
            settings.data_dir,
            config_fingerprint=config_fingerprint,
            open_browser=open_browser,
            wait_timeout=wait_timeout,
        )
    except _InstanceBusy as exc:
        if exc.record is None:
            reason = "the owning process has not published a complete startup record"
        elif exc.record.version != __version__:
            reason = (
                f"pid {exc.record.pid} reports version {exc.record.version}; "
                f"this command is version {__version__}"
            )
        elif exc.record.instance_id is None:
            reason = (
                f"pid {exc.record.pid} uses an older instance-lock protocol; "
                "stop that TraceForge process before starting this version"
            )
        elif exc.record.config_fingerprint != config_fingerprint:
            reason = (
                f"pid {exc.record.pid} at {exc.record.url} was started with a different "
                "workspace, host, or port configuration; stop it before changing the "
                "launch configuration"
            )
        else:
            reason = (
                f"pid {exc.record.pid} recorded {exc.record.url}, but its matching "
                "instance health endpoint is not ready or reachable"
            )
        typer.echo(
            "TraceForge could not start: another process holds the instance lock, "
            f"and {reason}.",
            err=True,
        )
        raise typer.Exit(code=1) from exc
    except OSError as exc:
        typer.echo(f"TraceForge could not secure its instance lock: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if instance_lock is None:
        return

    try:
        legacy_url = _find_legacy_instance(url, include_default=port != 8_765)
        if legacy_url is not None:
            _stop_for_legacy_instance(legacy_url)
        try:
            listener = _reserve_listener(host, port)
        except OSError as exc:
            if _legacy_server_ready(url):
                _stop_for_legacy_instance(url)
            try:
                retry_port = _suggested_port(host)
            except OSError:
                retry_port = port + 1 if port < 65_535 else port - 1
            retry = _retry_command(
                "serve",
                host=host,
                port=retry_port,
                workspace=workspace,
                open_browser=open_browser,
            )
            typer.echo(
                f"TraceForge could not bind {host}:{port}: {exc}. Retry with: {retry}",
                err=True,
            )
            raise typer.Exit(code=1) from exc

        with listener:
            instance_lock.publish(
                url,
                instance_id=instance_id,
                config_fingerprint=config_fingerprint,
            )
            from traceforge.api import create_app

            application = create_app(
                settings,
                instance_id=instance_id,
                instance_config_fingerprint=config_fingerprint,
            )
            typer.echo(f"Direct-task root: {settings.workspace}")
            typer.echo(f"Open {url}")
            if open_browser:
                _schedule_browser_open(
                    url,
                    instance_id=instance_id,
                )
            _run_server(application, listener, host=host, port=port)
    finally:
        instance_lock.close()


def _find_legacy_instance(requested_url: str, *, include_default: bool) -> str | None:
    candidates = [requested_url]
    default_url = _browser_url("127.0.0.1", 8_765)
    if include_default and default_url not in candidates:
        candidates.append(default_url)
    for candidate in candidates:
        if _legacy_server_ready(candidate):
            return candidate
    return None


def _stop_for_legacy_instance(url: str) -> None:
    typer.echo(
        "TraceForge could not start: an older TraceForge instance is already running "
        f"at {url} without the current instance-lock protocol. Stop that process before "
        "starting this version; a second server was not started.",
        err=True,
    )
    raise typer.Exit(code=1)


def _schedule_browser_open(url: str, *, instance_id: str) -> None:
    thread = Thread(
        target=_wait_for_server_and_open,
        args=(url,),
        kwargs={"instance_id": instance_id},
        daemon=True,
    )
    thread.start()


def _browser_url(host: str, port: int) -> str:
    browser_host = "127.0.0.1" if host == "0.0.0.0" else host
    if browser_host == "::":
        browser_host = "::1"
    if ":" in browser_host and not browser_host.startswith("["):
        browser_host = f"[{browser_host}]"
    return f"http://{browser_host}:{port}"


def _wait_for_server_and_open(
    url: str,
    *,
    timeout: float = 10.0,
    instance_id: str | None = None,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _server_ready(
            url,
            instance_id=instance_id,
        ):
            webbrowser.open(url)
            return True
        time.sleep(0.1)
    return False


def _health_target(url: str) -> tuple[str, int] | None:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            return None
        return parsed.hostname, parsed.port or 80
    except ValueError:
        return None


def _health_payload(url: str) -> dict[str, Any] | None:
    target = _health_target(url)
    if target is None:
        return None
    host, port = target
    connection: http.client.HTTPConnection | None = None
    try:
        connection = http.client.HTTPConnection(host, port, timeout=0.5)
        connection.request(
            "GET",
            "/healthz",
            headers={"Accept": "application/json", "Connection": "close"},
        )
        response = connection.getresponse()
        if response.status != 200:
            return None
        raw_payload = response.read(_HEALTH_RESPONSE_LIMIT + 1)
        if len(raw_payload) > _HEALTH_RESPONSE_LIMIT:
            return None
        payload: object = json.loads(raw_payload.decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except (
        OSError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        http.client.HTTPException,
    ):
        return None
    finally:
        if connection is not None:
            connection.close()


def _server_ready(
    url: str,
    *,
    instance_id: str | None = None,
) -> bool:
    payload = _health_payload(url)
    if payload is None:
        return False
    if payload.get("status") != "ok" or payload.get("version") != __version__:
        return False
    if instance_id is None:
        return True
    return payload.get("instance_id") == instance_id


def _legacy_server_ready(url: str) -> bool:
    payload = _health_payload(url)
    version = payload.get("version") if payload is not None else None
    return bool(
        payload is not None
        and payload.get("status") == "ok"
        and isinstance(version, str)
        and 0 < len(version.strip()) <= 128
        and "instance_id" not in payload
    )


@app.command()
def demo(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8765,
) -> None:
    """Launch a disposable, zero-credential demonstration workspace."""
    try:
        listener = _reserve_listener(host, port)
    except OSError as exc:
        try:
            retry_port = _suggested_port(host)
        except OSError:
            retry_port = port + 1 if port < 65_535 else port - 1
        retry = _retry_command("demo", host=host, port=retry_port)
        typer.echo(
            f"TraceForge demo could not bind {host}:{port}: {exc}. Retry with: {retry}",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    with listener:
        from traceforge.api import create_app
        from traceforge.config import Settings
        from traceforge.demo import DEMO_TASK, scripted_demo_provider

        development_source = (
            Path(__file__).resolve().parents[2] / "demo" / "tenant-cache-api"
        )
        packaged_source = resources.files("traceforge").joinpath("demo_workspace")
        source = development_source if development_source.is_dir() else packaged_source
        with resources.as_file(source) as source_path, TemporaryDirectory(
            prefix="traceforge-demo-"
        ) as temporary:
            temporary_root = Path(temporary)
            workspace = temporary_root / "tenant-cache-api"
            shutil.copytree(source_path, workspace)
            settings = Settings(
                workspace=workspace,
                data_dir=temporary_root / "data",
                api_key="",
                base_url=None,
                model="scripted-demo",
                suggested_task=DEMO_TASK,
                demo_mode=True,
            )
            url = _browser_url(host, port)
            instance_id = secrets.token_urlsafe(24)
            config_fingerprint = _instance_fingerprint(workspace, host=host, port=port)
            typer.echo(f"Demo workspace: {workspace}")
            typer.echo(f"Open {url} — the task is prefilled for you.")
            application = create_app(
                settings,
                provider=scripted_demo_provider(),
                instance_id=instance_id,
                instance_config_fingerprint=config_fingerprint,
            )
            _run_server(application, listener, host=host, port=port)

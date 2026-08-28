from __future__ import annotations

import asyncio
import json
import platform
import shutil
import socket
import sqlite3
import time
import webbrowser
from importlib import resources
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from threading import Thread
from typing import Annotated
from urllib.request import urlopen

import typer

from traceforge import __version__

app = typer.Typer(
    name="traceforge",
    help="A local coding agent that proves its work. Run without a command to launch the UI.",
    no_args_is_help=False,
    invoke_without_command=True,
)


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
    workspace: Path | None, *, host: str, port: int, open_browser: bool
) -> None:
    import uvicorn

    from traceforge.api import create_app
    from traceforge.config import Settings

    settings = Settings.from_env(workspace, require_api_key=False)
    url = _browser_url(host, port)
    typer.echo(f"Direct-task root: {settings.workspace}")
    typer.echo(f"Open {url}")
    if open_browser:
        _schedule_browser_open(url)
    uvicorn.run(create_app(settings), host=host, port=port)


def _schedule_browser_open(url: str) -> None:
    thread = Thread(target=_wait_for_server_and_open, args=(url,), daemon=True)
    thread.start()


def _browser_url(host: str, port: int) -> str:
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    if ":" in browser_host and not browser_host.startswith("["):
        browser_host = f"[{browser_host}]"
    return f"http://{browser_host}:{port}"


def _wait_for_server_and_open(url: str, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _server_ready(url):
            webbrowser.open(url)
            return True
        time.sleep(0.1)
    return False


def _server_ready(url: str) -> bool:
    try:
        with urlopen(f"{url}/healthz", timeout=0.5) as response:
            payload: object = json.load(response)
            if not isinstance(payload, dict):
                return False
            return all(
                (
                    response.status == 200,
                    payload.get("status") == "ok",
                    payload.get("version") == __version__,
                )
            )
    except (OSError, ValueError):
        return False


@app.command()
def demo(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8765,
) -> None:
    """Launch a disposable, zero-credential demonstration workspace."""
    import uvicorn

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
        typer.echo(f"Demo workspace: {workspace}")
        typer.echo(f"Open http://{host}:{port} — the task is prefilled for you.")
        uvicorn.run(
            create_app(settings, provider=scripted_demo_provider()), host=host, port=port
        )

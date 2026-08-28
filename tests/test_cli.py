from __future__ import annotations

import socket
import sqlite3
from contextlib import nullcontext
from types import SimpleNamespace

from typer.testing import CliRunner

import traceforge.config as config_module
from traceforge import __version__
from traceforge.cli import (
    _available_port,
    _browser_url,
    _server_ready,
    _wait_for_server_and_open,
    _writable_directory,
    app,
)
from traceforge.demo import DEMO_TASK
from traceforge.models import ProviderConfig
from traceforge.storage import Storage


def test_serve_and_demo_commands_build_runnable_apps(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    default_root = tmp_path / "TraceForge"
    monkeypatch.setattr(config_module, "user_data_path", lambda *args, **kwargs: data_dir)
    monkeypatch.setattr(config_module, "_default_workspace_path", lambda: default_root)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    servers: list[tuple[object, str, int]] = []
    opened: list[str] = []

    def fake_run(application, *, host: str, port: int) -> None:
        servers.append((application, host, port))
        application.state.storage.close()

    monkeypatch.setattr("uvicorn.run", fake_run)
    monkeypatch.setattr("traceforge.cli._schedule_browser_open", opened.append)
    runner = CliRunner()

    launched = runner.invoke(app, [])
    served_default = runner.invoke(
        app, ["serve", "--port", "9000", "--no-open-browser"]
    )
    served_override = runner.invoke(
        app,
        [
            "serve",
            "--workspace",
            str(workspace),
            "--host",
            "127.0.0.1",
            "--port",
            "9001",
            "--no-open-browser",
        ],
    )
    demonstrated = runner.invoke(app, ["demo", "--port", "9002"])

    assert launched.exit_code == 0, launched.output
    assert served_default.exit_code == 0, served_default.output
    assert served_override.exit_code == 0, served_override.output
    assert demonstrated.exit_code == 0, demonstrated.output
    assert servers[0][1:] == ("127.0.0.1", 8765)
    assert servers[0][0].state.settings.workspace == default_root.resolve()
    assert "Direct-task root:" in launched.output
    assert servers[1][1:] == ("127.0.0.1", 9000)
    assert servers[1][0].state.settings.workspace == default_root.resolve()
    assert servers[2][1:] == ("127.0.0.1", 9001)
    assert servers[2][0].state.settings.workspace == workspace.resolve()
    assert servers[3][1:] == ("127.0.0.1", 9002)
    assert servers[3][0].state.settings.suggested_task == DEMO_TASK
    assert servers[3][0].state.settings.demo_mode is True
    assert opened == ["http://127.0.0.1:8765"]
    assert "task is prefilled" in demonstrated.output


def test_cli_help_is_available() -> None:
    result = CliRunner().invoke(app, ["--help"])
    serve_help = CliRunner().invoke(app, ["serve", "--help"])

    assert result.exit_code == 0
    assert "local coding agent" in result.output
    assert "doctor" in result.output
    assert serve_help.exit_code == 0
    assert "[required]" not in serve_help.output


def test_browser_open_waits_for_the_traceforge_healthcheck(monkeypatch) -> None:
    readiness = iter([False, True])
    opened: list[str] = []
    monkeypatch.setattr("traceforge.cli._server_ready", lambda _url: next(readiness))
    monkeypatch.setattr("traceforge.cli.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("traceforge.cli.webbrowser.open", opened.append)

    assert _wait_for_server_and_open("http://127.0.0.1:8765", timeout=1) is True
    assert opened == ["http://127.0.0.1:8765"]
    assert _browser_url("0.0.0.0", 8765) == "http://127.0.0.1:8765"
    assert _browser_url("::", 8765) == "http://127.0.0.1:8765"
    assert _browser_url("::1", 8765) == "http://[::1]:8765"


def test_server_ready_rejects_an_unrelated_service(monkeypatch) -> None:
    response = SimpleNamespace(status=200)
    monkeypatch.setattr(
        "traceforge.cli.urlopen", lambda *_args, **_kwargs: nullcontext(response)
    )
    monkeypatch.setattr(
        "traceforge.cli.json.load", lambda _response: {"status": "ok", "version": "other"}
    )

    assert _server_ready("http://127.0.0.1:8765") is False

    monkeypatch.setattr(
        "traceforge.cli.json.load",
        lambda _response: {"status": "ok", "version": __version__},
    )
    assert _server_ready("http://127.0.0.1:8765") is True


def test_doctor_reports_readiness_without_exposing_credentials(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    default_root = tmp_path / "TraceForge"
    monkeypatch.setattr(config_module, "user_data_path", lambda *args, **kwargs: data_dir)
    monkeypatch.setattr(config_module, "_default_workspace_path", lambda: default_root)
    monkeypatch.setattr("traceforge.cli.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "traceforge.cli._available_port",
        lambda host, port: (True, f"{host}:{port}"),
    )
    monkeypatch.setattr(
        "traceforge.sandbox.sandbox_status",
        lambda _workspace: SimpleNamespace(
            enforced=True,
            backend="seatbelt",
            detail="Seatbelt is enforced.",
        ),
    )
    credential = tmp_path / "deepseek.key"
    credential.write_text("never-print-this-value")
    credential.chmod(0o600)
    storage = Storage(data_dir / "traceforge.db")
    storage.save_provider_config(
        ProviderConfig(model="test-model", credential_file=str(credential))
    )
    storage.close()

    async def successful_probe(_runtime):
        return {"ok": True, "detail": "Connection and native tool calling verified."}

    monkeypatch.setattr(
        "traceforge.runtime.AgentRuntime.test_connection", successful_probe
    )

    result = CliRunner().invoke(
        app,
        ["doctor", "--probe-model"],
    )

    assert result.exit_code == 0, result.output
    assert "[PASS] Direct-task root write" in result.output
    assert "[PASS] Command sandbox" in result.output
    assert "[PASS] Model credential" in result.output
    assert "[PASS] Model probe" in result.output
    assert "READY TO SERVE" in result.output
    assert default_root.is_dir()
    assert "never-print-this-value" not in result.output


def test_doctor_fails_for_blocked_port_and_required_sandbox(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(config_module, "user_data_path", lambda *args, **kwargs: data_dir)
    monkeypatch.setattr("traceforge.cli.platform.system", lambda: "Linux")
    monkeypatch.setattr(
        "traceforge.cli._available_port",
        lambda _host, _port: (False, "address already in use"),
    )
    monkeypatch.setattr(
        "traceforge.sandbox.sandbox_status",
        lambda _workspace: SimpleNamespace(
            enforced=False,
            backend="policy_only",
            detail="No OS sandbox backend passed its probe.",
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "doctor",
            "--workspace",
            str(workspace),
            "--require-os-sandbox",
            "--probe-model",
        ],
    )

    assert result.exit_code == 1
    assert "[FAIL] Listen address · address already in use" in result.output
    assert "[FAIL] Command sandbox" in result.output
    assert "[FAIL] Model probe · credential is not ready" in result.output
    assert "NOT READY · 3 failure(s)" in result.output


def test_doctor_reports_an_unreadable_state_database(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(config_module, "user_data_path", lambda *args, **kwargs: data_dir)
    monkeypatch.setattr("traceforge.cli.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "traceforge.cli._available_port",
        lambda host, port: (True, f"{host}:{port}"),
    )
    monkeypatch.setattr(
        "traceforge.sandbox.sandbox_status",
        lambda _workspace: SimpleNamespace(
            enforced=True,
            backend="seatbelt",
            detail="Seatbelt is enforced.",
        ),
    )

    def broken_storage(_path):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr("traceforge.storage.Storage", broken_storage)

    result = CliRunner().invoke(app, ["doctor", "--workspace", str(workspace)])

    assert result.exit_code == 1
    assert "[FAIL] State database · database disk image is malformed" in result.output
    assert "NOT READY · 1 failure(s)" in result.output


def test_doctor_checks_real_directory_and_port_boundaries(tmp_path) -> None:
    writable, detail = _writable_directory(tmp_path)
    assert writable is True
    assert detail == str(tmp_path)
    missing, _ = _writable_directory(tmp_path / "missing")
    assert missing is False

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        available, error = _available_port("127.0.0.1", port)
        assert available is False
        assert "in use" in error.lower()

    available, detail = _available_port("127.0.0.1", port)
    assert available is True
    assert detail == f"127.0.0.1:{port}"

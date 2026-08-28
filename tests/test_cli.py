from __future__ import annotations

import json
import socket
import sqlite3
import stat
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import traceforge.config as config_module
from traceforge import __version__
from traceforge.cli import (
    _INSTANCE_LOCK_NAME,
    _acquire_instance_lock,
    _acquire_or_reuse_instance,
    _available_port,
    _browser_url,
    _health_payload,
    _instance_fingerprint,
    _InstanceBusy,
    _legacy_server_ready,
    _reserve_listener,
    _run_server,
    _server_ready,
    _wait_for_server_and_open,
    _writable_directory,
    app,
)
from traceforge.demo import DEMO_TASK
from traceforge.models import ProviderConfig, RunRecord, RunState, ToolCall
from traceforge.provider import ModelResponse
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
    held_lock_checks = 0

    def fake_run(application, _listener, *, host: str, port: int) -> None:
        nonlocal held_lock_checks
        servers.append((application, host, port))
        if not application.state.settings.demo_mode:
            with pytest.raises(_InstanceBusy):
                _acquire_instance_lock(data_dir)
            held_lock_checks += 1
        application.state.storage.close()

    monkeypatch.setattr(
        "traceforge.cli._reserve_listener",
        lambda _host, _port: socket.socket(socket.AF_INET, socket.SOCK_STREAM),
    )
    monkeypatch.setattr("traceforge.cli._legacy_server_ready", lambda _url: False)
    monkeypatch.setattr("traceforge.cli._run_server", fake_run)
    monkeypatch.setattr(
        "traceforge.cli._schedule_browser_open",
        lambda url, **_kwargs: opened.append(url),
    )
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
    assert servers[3][0].state.instance_id
    assert servers[3][0].state.instance_config_fingerprint
    assert opened == ["http://127.0.0.1:8765"]
    assert "task is prefilled" in demonstrated.output
    assert held_lock_checks == 3


def test_run_server_hands_the_reserved_listener_to_uvicorn(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_config(application, *, host: str, port: int):
        captured["application"] = application
        captured["address"] = (host, port)
        return object()

    class FakeServer:
        def __init__(self, config) -> None:
            captured["config"] = config

        def run(self, *, sockets) -> None:
            captured["sockets"] = sockets

    monkeypatch.setattr("uvicorn.Config", fake_config)
    monkeypatch.setattr("uvicorn.Server", FakeServer)
    application = object()
    with _reserve_listener("127.0.0.1", 0) as listener:
        port = listener.getsockname()[1]
        _run_server(application, listener, host="127.0.0.1", port=port)
        assert captured["sockets"] == [listener]

    assert captured["application"] is application
    assert captured["address"] == ("127.0.0.1", port)


def test_serve_reuses_a_healthy_same_version_instance_without_touching_runs(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(config_module, "user_data_path", lambda *args, **kwargs: data_dir)
    storage = Storage(data_dir / "traceforge.db")
    storage.create_run(
        RunRecord(
            id="active-run",
            task="Keep running",
            workspace=str(workspace),
            state=RunState.EXECUTING,
        )
    )
    existing_url = "http://127.0.0.1:9876"
    config_fingerprint = _instance_fingerprint(
        workspace.resolve(), host="127.0.0.1", port=9876
    )
    existing = _acquire_instance_lock(data_dir)
    existing.publish(
        existing_url,
        instance_id="existing-instance-id",
        config_fingerprint=config_fingerprint,
    )
    opened: list[str] = []
    create_app_calls = 0

    def forbidden_create_app(*_args, **_kwargs):
        nonlocal create_app_calls
        create_app_calls += 1
        raise AssertionError("a second process must not construct the application")

    monkeypatch.setattr(
        "traceforge.cli._server_ready", lambda url, **_kwargs: url == existing_url
    )
    monkeypatch.setattr("traceforge.cli.webbrowser.open", opened.append)
    monkeypatch.setattr("traceforge.api.create_app", forbidden_create_app)
    try:
        result = CliRunner().invoke(
            app,
            ["serve", "--workspace", str(workspace), "--port", "9876"],
        )
        result_without_browser = CliRunner().invoke(
            app,
            [
                "serve",
                "--workspace",
                str(workspace),
                "--port",
                "9876",
                "--no-open-browser",
            ],
        )
    finally:
        existing.close()

    assert result.exit_code == 0, result.output
    assert result_without_browser.exit_code == 0, result_without_browser.output
    assert f"already running at {existing_url}" in result.output
    assert opened == [existing_url]
    assert create_app_calls == 0
    assert storage.get_run("active-run").state is RunState.EXECUTING
    storage.close()


def test_busy_lock_waits_for_owner_to_publish_before_reusing(
    tmp_path, monkeypatch
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fingerprint = _instance_fingerprint(
        workspace.resolve(), host="127.0.0.1", port=8765
    )
    owner = _acquire_instance_lock(data_dir)
    published = False

    def publish_after_first_attempt(_seconds: float) -> None:
        nonlocal published
        if not published:
            owner.publish(
                "http://127.0.0.1:8765",
                instance_id="owner-instance-id",
                config_fingerprint=fingerprint,
            )
            published = True

    monkeypatch.setattr("traceforge.cli.time.sleep", publish_after_first_attempt)
    monkeypatch.setattr("traceforge.cli._server_ready", lambda _url, **_kwargs: True)
    try:
        claimed = _acquire_or_reuse_instance(
            data_dir,
            config_fingerprint=fingerprint,
            open_browser=False,
            wait_timeout=1.0,
        )
    finally:
        owner.close()

    assert claimed is None
    assert published is True


def test_busy_lock_retries_health_until_published_owner_is_ready(
    tmp_path, monkeypatch
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fingerprint = _instance_fingerprint(
        workspace.resolve(), host="127.0.0.1", port=8765
    )
    owner = _acquire_instance_lock(data_dir)
    owner.publish(
        "http://127.0.0.1:8765",
        instance_id="owner-instance-id",
        config_fingerprint=fingerprint,
    )
    readiness = iter([False, True])
    monkeypatch.setattr("traceforge.cli.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "traceforge.cli._server_ready",
        lambda _url, **_kwargs: next(readiness),
    )
    try:
        claimed = _acquire_or_reuse_instance(
            data_dir,
            config_fingerprint=fingerprint,
            open_browser=False,
            wait_timeout=1.0,
        )
    finally:
        owner.close()

    assert claimed is None


def test_busy_lock_takes_over_when_starting_owner_exits(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fingerprint = _instance_fingerprint(
        workspace.resolve(), host="127.0.0.1", port=8765
    )
    owner = _acquire_instance_lock(data_dir)
    monkeypatch.setattr("traceforge.cli.time.sleep", lambda _seconds: owner.close())

    claimed = _acquire_or_reuse_instance(
        data_dir,
        config_fingerprint=fingerprint,
        open_browser=False,
        wait_timeout=1.0,
    )

    assert claimed is not None
    claimed.close()


def test_serve_rejects_a_healthy_instance_with_different_launch_configuration(
    tmp_path, monkeypatch
) -> None:
    first_workspace = tmp_path / "first"
    first_workspace.mkdir()
    requested_workspace = tmp_path / "requested"
    requested_workspace.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(config_module, "user_data_path", lambda *args, **kwargs: data_dir)
    owner = _acquire_instance_lock(data_dir)
    owner.publish(
        "http://127.0.0.1:8765",
        instance_id="owner-instance-id",
        config_fingerprint=_instance_fingerprint(
            first_workspace.resolve(), host="127.0.0.1", port=8765
        ),
    )
    monkeypatch.setattr(
        "traceforge.cli._server_ready",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a configuration mismatch must fail before health reuse")
        ),
    )
    try:
        result = CliRunner().invoke(
            app,
            [
                "serve",
                "--workspace",
                str(requested_workspace),
                "--no-open-browser",
            ],
        )
    finally:
        owner.close()

    assert result.exit_code == 1
    assert "different workspace, host, or port configuration" in result.output


def test_serve_fails_closed_for_a_locked_unreachable_instance(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(config_module, "user_data_path", lambda *args, **kwargs: data_dir)
    storage = Storage(data_dir / "traceforge.db")
    storage.create_run(
        RunRecord(
            id="active-run",
            task="Keep running",
            workspace=str(workspace),
            state=RunState.EXECUTING,
        )
    )
    config_fingerprint = _instance_fingerprint(
        workspace.resolve(), host="127.0.0.1", port=9876
    )
    existing = _acquire_instance_lock(data_dir)
    existing.publish(
        "http://127.0.0.1:9876",
        instance_id="existing-instance-id",
        config_fingerprint=config_fingerprint,
    )
    create_app_calls = 0

    def forbidden_create_app(*_args, **_kwargs):
        nonlocal create_app_calls
        create_app_calls += 1
        raise AssertionError("a locked startup must not construct the application")

    monkeypatch.setattr("traceforge.cli._server_ready", lambda _url, **_kwargs: False)
    monkeypatch.setattr("traceforge.cli._INSTANCE_STARTUP_WAIT_SECONDS", 0.0)
    monkeypatch.setattr("traceforge.api.create_app", forbidden_create_app)
    monkeypatch.setattr("traceforge.cli._legacy_server_ready", lambda _url: False)
    try:
        result = CliRunner().invoke(
            app,
            [
                "serve",
                "--workspace",
                str(workspace),
                "--port",
                "9876",
                "--no-open-browser",
            ],
        )
    finally:
        existing.close()

    assert result.exit_code == 1
    assert "another process holds the instance lock" in result.output
    assert "not ready or reachable" in result.output
    assert create_app_calls == 0
    assert storage.get_run("active-run").state is RunState.EXECUTING
    storage.close()


def test_custom_port_startup_refuses_a_legacy_default_instance(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(config_module, "user_data_path", lambda *args, **kwargs: data_dir)
    checked: list[str] = []

    def legacy_ready(url: str) -> bool:
        checked.append(url)
        return url == "http://127.0.0.1:8765"

    monkeypatch.setattr("traceforge.cli._legacy_server_ready", legacy_ready)
    monkeypatch.setattr(
        "traceforge.cli._reserve_listener",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy detection must happen before binding another port")
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "serve",
            "--workspace",
            str(workspace),
            "--port",
            "9876",
            "--no-open-browser",
        ],
    )

    assert result.exit_code == 1
    assert checked == ["http://127.0.0.1:9876", "http://127.0.0.1:8765"]
    assert "older TraceForge instance" in result.output
    assert "Stop that process" in result.output
    replacement = _acquire_instance_lock(data_dir)
    replacement.close()


def test_bind_race_with_a_legacy_instance_never_suggests_a_parallel_port(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(config_module, "user_data_path", lambda *args, **kwargs: data_dir)
    readiness = iter([False, True])
    monkeypatch.setattr(
        "traceforge.cli._legacy_server_ready", lambda _url: next(readiness)
    )
    monkeypatch.setattr(
        "traceforge.cli._reserve_listener",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("address in use")),
    )

    result = CliRunner().invoke(
        app,
        ["serve", "--workspace", str(workspace), "--no-open-browser"],
    )

    assert result.exit_code == 1
    assert "older TraceForge instance" in result.output
    assert "Retry with:" not in result.output


def test_current_demo_identity_is_not_legacy_and_port_conflict_suggests_retry(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(config_module, "user_data_path", lambda *args, **kwargs: data_dir)
    monkeypatch.setattr(
        "traceforge.cli._health_payload",
        lambda _url: {
            "status": "ok",
            "version": __version__,
            "instance_id": "current-demo-instance",
        },
    )
    monkeypatch.setattr(
        "traceforge.cli._reserve_listener",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("address in use")),
    )
    monkeypatch.setattr("traceforge.cli._suggested_port", lambda _host: 9_001)

    result = CliRunner().invoke(
        app,
        ["serve", "--workspace", str(workspace), "--no-open-browser"],
    )

    assert result.exit_code == 1
    assert "older TraceForge instance" not in result.output
    assert "Retry with: uv run traceforge serve" in result.output
    assert "--port 9001" in result.output


def test_occupied_port_fails_before_serve_or_demo_constructs_an_app(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(config_module, "user_data_path", lambda *args, **kwargs: data_dir)
    storage = Storage(data_dir / "traceforge.db")
    storage.create_run(
        RunRecord(
            id="active-run",
            task="Keep running",
            workspace=str(workspace),
            state=RunState.EXECUTING,
        )
    )
    create_app_calls = 0

    def forbidden_create_app(*_args, **_kwargs):
        nonlocal create_app_calls
        create_app_calls += 1
        raise AssertionError("an occupied port must fail before application construction")

    monkeypatch.setattr("traceforge.api.create_app", forbidden_create_app)
    monkeypatch.setattr("traceforge.cli._legacy_server_ready", lambda _url: False)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        served = CliRunner().invoke(
            app,
            [
                "serve",
                "--workspace",
                str(workspace),
                "--port",
                str(port),
                "--no-open-browser",
            ],
        )
        demonstrated = CliRunner().invoke(app, ["demo", "--port", str(port)])

    assert served.exit_code == 1
    assert f"could not bind 127.0.0.1:{port}" in served.output
    assert "Retry with: uv run traceforge serve" in served.output
    assert "--port" in served.output
    assert demonstrated.exit_code == 1
    assert f"demo could not bind 127.0.0.1:{port}" in demonstrated.output
    assert "Retry with: uv run traceforge demo --port" in demonstrated.output
    assert create_app_calls == 0
    assert storage.get_run("active-run").state is RunState.EXECUTING
    storage.close()


def test_instance_lock_is_owner_only_minimal_and_symlink_safe(tmp_path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    instance = _acquire_instance_lock(data_dir)
    instance.publish(
        "http://127.0.0.1:8765",
        instance_id="owner-instance-id",
        config_fingerprint="a" * 64,
    )
    lock_path = data_dir / _INSTANCE_LOCK_NAME
    try:
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
        payload = json.loads(lock_path.read_text())
        assert set(payload) == {
            "config_fingerprint",
            "instance_id",
            "pid",
            "url",
            "version",
        }
        assert payload["url"] == "http://127.0.0.1:8765"
        assert payload["version"] == __version__
        assert payload["instance_id"] == "owner-instance-id"
        assert payload["config_fingerprint"] == "a" * 64
    finally:
        instance.close()

    victim = tmp_path / "victim"
    victim.write_text("leave me alone")
    lock_path.unlink()
    lock_path.symlink_to(victim)
    with pytest.raises(OSError):
        _acquire_instance_lock(data_dir)
    assert victim.read_text() == "leave me alone"


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
    monkeypatch.setattr(
        "traceforge.cli._server_ready", lambda _url, **_kwargs: next(readiness)
    )
    monkeypatch.setattr("traceforge.cli.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("traceforge.cli.webbrowser.open", opened.append)

    assert _wait_for_server_and_open("http://127.0.0.1:8765", timeout=1) is True
    assert opened == ["http://127.0.0.1:8765"]
    assert _browser_url("0.0.0.0", 8765) == "http://127.0.0.1:8765"
    assert _browser_url("::", 8765) == "http://[::1]:8765"
    assert _browser_url("::1", 8765) == "http://[::1]:8765"


def test_server_ready_requires_exact_instance_identity_for_reuse(monkeypatch) -> None:
    payload: dict[str, object] = {"status": "ok", "version": "other"}
    monkeypatch.setattr("traceforge.cli._health_payload", lambda _url: payload)
    assert _server_ready("http://127.0.0.1:8765") is False

    payload.update({"version": __version__})
    assert _server_ready("http://127.0.0.1:8765") is True
    payload.update({"version": "0.0.1"})
    assert _legacy_server_ready("http://127.0.0.1:8765") is True
    payload.update({"version": __version__})
    assert (
        _server_ready(
            "http://127.0.0.1:8765",
            instance_id="expected-instance-id",
        )
        is False
    )

    payload.update({"instance_id": "expected-instance-id"})
    assert _legacy_server_ready("http://127.0.0.1:8765") is False
    assert _server_ready(
        "http://127.0.0.1:8765",
        instance_id="expected-instance-id",
    )
    assert not _server_ready(
        "http://127.0.0.1:8765",
        instance_id="different-instance-id",
    )


def test_health_probe_is_direct_bounded_and_does_not_follow_redirects(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        status = 200
        body = json.dumps({"status": "ok", "version": __version__}).encode()

        def read(self, limit: int) -> bytes:
            captured["read_limit"] = limit
            return self.body

    class FakeConnection:
        def __init__(self, host: str, port: int, *, timeout: float) -> None:
            captured["connection"] = (host, port, timeout)

        def request(self, method: str, path: str, *, headers) -> None:
            captured["request"] = (method, path, headers)

        def getresponse(self):
            return FakeResponse()

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr("traceforge.cli.http.client.HTTPConnection", FakeConnection)

    assert _health_payload("http://[::1]:8765") == {
        "status": "ok",
        "version": __version__,
    }
    assert captured["connection"] == ("::1", 8765, 0.5)
    assert captured["read_limit"] == 8 * 1024 + 1
    assert captured["closed"] is True

    FakeResponse.body = b"x" * (8 * 1024 + 1)
    assert _health_payload("http://127.0.0.1:8765") is None

    FakeResponse.status = 302
    assert _health_payload("http://127.0.0.1:8765") is None


@pytest.mark.parametrize("host", ["::", "::1"])
def test_ipv6_listener_matches_the_browser_loopback_url(host: str) -> None:
    if not socket.has_ipv6:
        pytest.skip("IPv6 is unavailable")
    try:
        listener = _reserve_listener(host, 0)
    except OSError:
        pytest.skip("IPv6 loopback is unavailable")
    with listener:
        port = listener.getsockname()[1]
        with socket.create_connection(("::1", port), timeout=0.5):
            pass
        assert _browser_url(host, port) == f"http://[::1]:{port}"


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


def test_successful_doctor_model_probe_persists_connection_verification(
    tmp_path, monkeypatch
) -> None:
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
    monkeypatch.setenv("OPENAI_API_KEY", "doctor-test-key")

    class SuccessfulProbeProvider:
        def __init__(self, _settings) -> None:
            pass

        async def complete(self, messages, tools=None) -> ModelResponse:
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="probe",
                        name="report_connection",
                        arguments={"status": "ok"},
                    )
                ]
            )

    monkeypatch.setattr(
        "traceforge.runtime.OpenAICompatibleProvider", SuccessfulProbeProvider
    )

    result = CliRunner().invoke(app, ["doctor", "--probe-model"])

    assert result.exit_code == 0, result.output
    storage = Storage(data_dir / "traceforge.db")
    try:
        assert storage.get_provider_verified_at() is not None
    finally:
        storage.close()


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

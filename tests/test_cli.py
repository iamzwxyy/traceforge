from __future__ import annotations

from typer.testing import CliRunner

import traceforge.config as config_module
from traceforge.cli import app
from traceforge.demo import DEMO_TASK


def test_serve_and_demo_commands_build_runnable_apps(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_dir = tmp_path / "data"
    monkeypatch.setattr(config_module, "user_data_path", lambda *args, **kwargs: data_dir)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    launched: list[tuple[object, str, int]] = []

    def fake_run(application, *, host: str, port: int) -> None:
        launched.append((application, host, port))
        application.state.storage.close()

    monkeypatch.setattr("uvicorn.run", fake_run)
    runner = CliRunner()

    served = runner.invoke(
        app,
        ["serve", "--workspace", str(workspace), "--host", "127.0.0.1", "--port", "9001"],
    )
    demonstrated = runner.invoke(app, ["demo", "--port", "9002"])

    assert served.exit_code == 0, served.output
    assert demonstrated.exit_code == 0, demonstrated.output
    assert launched[0][1:] == ("127.0.0.1", 9001)
    assert launched[1][1:] == ("127.0.0.1", 9002)
    assert launched[1][0].state.settings.suggested_task == DEMO_TASK
    assert "task is prefilled" in demonstrated.output


def test_cli_help_is_available() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "local coding agent" in result.output

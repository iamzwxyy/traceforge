from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

import typer

app = typer.Typer(
    name="traceforge",
    help="A local coding agent that proves its work.",
    no_args_is_help=True,
)


@app.command()
def serve(
    workspace: Annotated[
        Path, typer.Option(exists=True, file_okay=False, resolve_path=True)
    ],
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8765,
) -> None:
    """Run the local TraceForge web application."""
    import uvicorn

    from traceforge.api import create_app
    from traceforge.config import Settings

    settings = Settings.from_env(workspace, require_api_key=False)
    uvicorn.run(create_app(settings), host=host, port=port)


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
        )
        typer.echo(f"Demo workspace: {workspace}")
        typer.echo(f"Open http://{host}:{port} — the task is prefilled for you.")
        uvicorn.run(
            create_app(settings, provider=scripted_demo_provider()), host=host, port=port
        )

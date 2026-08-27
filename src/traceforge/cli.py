from __future__ import annotations

from pathlib import Path
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

    settings = Settings.from_env(workspace)
    uvicorn.run(create_app(settings), host=host, port=port)


@app.command()
def demo(
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8765,
) -> None:
    """Launch a disposable copy of the bundled demonstration workspace."""
    raise typer.BadParameter("The demonstration workspace is not packaged yet")

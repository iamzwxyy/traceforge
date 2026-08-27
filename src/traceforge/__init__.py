"""TraceForge: a local coding agent that proves its work."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("traceforge-agent")
except PackageNotFoundError:
    __version__ = "0.1.0"

__all__ = ["__version__"]


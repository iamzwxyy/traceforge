# TraceForge repository guidance

## Product constraints

- Build a local, explainable coding agent. Do not use agent frameworks or hosted code/file execution tools.
- Keep the agent core independent from FastAPI and React so it can be tested without the web application.
- Use OpenAI-compatible native tool calling. Own history management, tool dispatch, approvals, termination, recovery, and verification in this repository.
- Never read, print, store, or commit API keys. Configuration comes from environment variables only.
- Support macOS and Linux. Do not add Windows-specific branches in v1.

## Engineering expectations

- Python code targets 3.12 and must pass `uv run ruff check .`, `uv run mypy src`, and `uv run pytest`.
- Frontend code must pass `pnpm --dir web lint`, `pnpm --dir web typecheck`, and `pnpm --dir web test -- --run`.
- Run the narrowest relevant tests while iterating, then the complete validation suite before claiming completion.
- New behavior requires tests. Security boundaries require adversarial tests.
- Keep commits focused and preserve the visible development history.

## Definition of done

- A fresh checkout can install and launch TraceForge using the documented commands.
- The deterministic fake-provider flow covers clarification, plan approval, edits, checks, verification, and rollback without an API key.
- The UI visibly distinguishes evidence from model-authored summaries and never exposes hidden chain-of-thought.


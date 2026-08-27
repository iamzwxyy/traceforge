# TraceForge repository guidance

## Product constraints

- Build a local, explainable coding agent. Do not use agent frameworks or hosted code/file execution tools.
- Keep the agent core independent from FastAPI and React so it can be tested without the web application.
- Use OpenAI-compatible native tool calling. Own history management, tool dispatch, approvals, termination, recovery, and verification in this repository.
- Never print, persist, expose, or commit API-key values. Provider credentials may come from the
  process environment or an owner-only local file reference; store only the file path, resolve the
  value inside provider construction, and scrub credential-like variables from child commands.
- Support macOS and Linux. Do not add Windows-specific branches in v1.

## Engineering expectations

- Python code targets 3.12 and must pass `uv run ruff check .`, `uv run mypy src`, and `uv run pytest`.
- Frontend code must pass `pnpm --filter traceforge-web lint`, `pnpm --filter traceforge-web typecheck`, and `pnpm --filter traceforge-web test --run`.
- Run the narrowest relevant tests while iterating, then the complete validation suite before claiming completion.
- New behavior requires tests. Security boundaries require adversarial tests.
- Keep commits focused and preserve the visible development history.

## Definition of done

- A fresh checkout can install and launch TraceForge using the documented commands.
- The deterministic fake-provider flow covers clarification, plan approval, edits, checks, verification, and rollback without an API key.
- The UI visibly distinguishes evidence from model-authored summaries and never exposes hidden chain-of-thought.

# TraceForge

TraceForge is a local coding agent that makes its completion evidence visible. It turns a task into an approved plan, edits a bounded workspace, runs explicit checks, and asks an independent read-only verifier to assess the diff and command evidence.

> The project is under active construction. See [PLAN.md](PLAN.md) for the agreed product contract.

## Safety baseline

- Model credentials are read only from environment variables.
- File tools cannot escape the selected workspace or write into `.git`.
- Commands use argv execution rather than a shell and unknown or risky actions require approval.
- Every agent-authored file change is snapshotted for conflict-aware whole-run rollback.


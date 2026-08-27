# TraceForge architecture

## Design objective

TraceForge optimizes for a claim that can be defended: “the requested change is complete because
these files changed, these checks passed, and an independent component reviewed the same
evidence.” The local application therefore treats state, tool results, diffs, and verification as
first-class data rather than rendering a chat transcript around an opaque model loop.

## Component boundaries

```mermaid
flowchart TB
    UI[React workbench] <-->|REST + sequenced WebSocket| API[FastAPI]
    API --> Runtime[AgentRuntime workspace router]
    Runtime --> Manager[AgentManager per active workspace]
    Manager --> Provider[ModelProvider]
    Manager --> Gate[Deterministic plan gate]
    Manager --> Tools[ToolRegistry]
    Manager --> Context[ContextManager]
    Tools --> Guard[Workspace guard + snapshots]
    Tools --> Process[argv subprocess runner]
    Process --> Sandbox[Seatbelt / Bubblewrap / explicit policy-only]
    Manager --> Broker[EventBroker]
    Broker --> DB[(SQLite WAL)]
    Guard --> DB
    DB --> Proof[Proof Pack projection]
```

- `ModelProvider` only translates OpenAI-compatible tool calls. It does not own the loop.
- `AgentRuntime` resolves canonical workspace roots and lazily creates one manager per directory,
  allowing independent workspaces to progress concurrently without overlapping writers.
- `AgentManager` is the product core: phases, approvals, retries, evidence freshness, repair
  cycles, cancellation, resume, and completion.
- `assess_plan_gate` is a deterministic policy projection over the task and structured plan; the
  model cannot label its own work low-risk.
- `ToolRegistry` defines and executes the bounded local capability surface.
- `CommandSandbox` probes an OS backend and constructs a per-command profile; it returns structured
  enforcement metadata even when execution is policy-only or explicitly bypassed.
- `Workspace` resolves paths, records the first pre-change snapshot, creates diffs, and rolls back.
- `EventBroker` persists an event before publishing it, so reconnecting clients cannot miss a
  transition.
- FastAPI exposes public run views without internal model messages or credentials.

## State machine

```mermaid
stateDiagram-v2
    [*] --> created
    created --> planning
    planning --> awaiting_clarification
    awaiting_clarification --> planning
    planning --> awaiting_plan_approval
    planning --> executing: deterministic low-risk fast path
    awaiting_plan_approval --> planning: revise
    awaiting_plan_approval --> executing: approve
    executing --> awaiting_action_approval: unknown action
    awaiting_action_approval --> executing: allow or reject
    executing --> verifying: finish + fresh checks
    verifying --> executing: findings and repair budget
    verifying --> succeeded: pass
    verifying --> failed: repair limit
    created --> cancelled
    planning --> cancelled
    executing --> cancelled
    created --> interrupted: process exit
    planning --> interrupted: process exit
    executing --> interrupted: process exit
    interrupted --> planning: resume before plan
    interrupted --> awaiting_plan_approval: resume at plan
    interrupted --> executing: resume persisted fast path
    interrupted --> executing: resume approved work
    succeeded --> rolled_back
    failed --> rolled_back
    cancelled --> rolled_back
    interrupted --> rolled_back
```

Transitions are allowlisted. Invalid public actions return a conflict rather than silently
changing state. A process shutdown records the previous phase and never automatically replays an
incomplete tool call. On resume, any unmatched assistant tool call receives a synthetic failure
result before the model is called again, preserving the provider protocol.

## Clarify, plan, build, verify

### Planning

The planner can only list, read, and search. Material ambiguity is represented as structured
questions: at most three per round, two to four options per question, and at most two rounds. A
validated `TaskPlan` contains steps, an explicit relative file scope, acceptance checks, optional
argv commands, and risks.

Malformed structured clarification or plan calls are returned as failed tool results with
field-level schema errors that omit invalid input values. They remain auditable and can be corrected
within the same bounded planning phase instead of terminating the run.

After validation, application code grades the plan. Automatic approval requires exactly one
declared file, at most four steps and checks, no clarification, no sensitive-area language in task
or risk notes, and only recognized local verification commands. Generic low-impact caveats do not
disable the fast path. Every other plan enters the visible
approval state. The decision and reasons are persisted as a `PlanGate`; a restart preserves an
already-recorded fast-path decision. Any later file mutation outside `impacted_files` is a separate
action approval, so automatic plan approval does not silently broaden the scope.

### Building

The builder receives the original task and approved plan. It can call six local file/process tools
plus the structured `update_plan` and `finish` controls. File mutations invalidate prior command
evidence. Exact acceptance commands and known read-only commands enter the OS sandbox when its
probe succeeds. Non-writing, non-interactive focused Pytest variants from the same launcher family
also stay sandboxed without another prompt, but only the exact planned argv refreshes acceptance
evidence. Other check-family changes pause for a decision.
An unknown executable action pauses; user approval grants one visibly recorded unsandboxed
invocation rather than silently weakening later commands. `finish` is rejected until
every command-backed acceptance check has a fresh passing result.

The loop has three independent brakes:

1. 30 tool steps by default.
2. Two identical failures trigger a recovery instruction; a third ends the run.
3. A builder that twice responds without a tool or `finish` fails explicitly.

### Verifying

The verifier receives the original task, approved plan, current diff, and recent persisted tool
events. Its available local tools are read-only. Invalid structured reports are returned as tool
errors for correction. Mixed read-and-submit turns must finish the reads and submit one verdict in
a later turn, keeping Chat Completions tool-call/result ordering valid.

A `pass` completes the run. `fail` or `inconclusive` becomes a builder repair instruction. The
default repair budget is two; exhaustion produces an honest failed state.

## Evidence freshness

Every acceptance command is stored as an argv list, not a shell string. A successful result saves
its exit code and tail evidence on the matching check. Any later `apply_patch` or `create_file`
sets command-backed checks to pending with “files changed after the previous check.” This prevents
the agent from citing stale tests after a repair.

Tool output has two limits: at most 1 MiB is persisted and at most 16 KiB (head and tail) is sent
back to the model. The complete public status still includes exit code, timeout, truncation, argv,
working directory, and per-command sandbox metadata.

## Proof Pack projection

`GET /api/runs/{id}/proof-pack` assembles a versioned evidence projection from the run, snapshots,
and sequenced events. For completed work it prefers the diff stored in the completion event; for
earlier terminal states it uses the last persisted diff event, falling back to the live workspace
only when no persisted diff exists. Later user edits therefore do not rewrite a successful run's
historical delivery evidence. It also counts commands by enforced, bypassed, and policy-only
execution, while rejected or denied commands are counted separately as not executed. The exported
artifact therefore cannot imply stronger isolation—or more executed commands—than the event ledger
proves.

The projection includes the visible plan and gate, touched paths, final diff, fresh check results,
independent verdict, rollback outcome, counts, and two SHA-256 values: one for the event sequence
and one for the assembled evidence. The Markdown route renders the same projection as a download.
These hashes detect accidental or post-export changes; they are not signatures or a defense
against a local user deliberately rewriting both SQLite and the artifact.

## Context management

TraceForge estimates tokens deterministically from serialized UTF-8 bytes. At 70% of the configured
window, and only when history is long enough, it retains the first two messages, the newest twelve,
and inserts an evidence-oriented summary of the middle. Tool names and bounded results are kept;
hidden reasoning is neither requested nor displayed.

## Persistence and recovery

SQLite uses WAL mode and six tables:

- `runs`: public state, optional project association, internal messages, plan and deterministic
  gate, approval state, and interrupted origin;
- `events`: per-run monotonically increasing sequence and JSON payload;
- `snapshots`: original bytes, mode, original hash, and last agent-written hash per path.
- `projects`: reusable display names and canonical local root directories;
- `provider_config`: one model/base-URL/credential-file reference, never the secret value;
- `preferences`: small local UI choices such as the last direct-task workspace.

Startup migrations add compatible columns to early v0.1 databases. WebSocket clients request
events after their last sequence; the server replays persisted rows before subscribing to new
ones. One workspace permits one active or interrupted run to avoid overlapping writes; different
workspaces use separate managers and may run concurrently. On process startup, every unfinished
run is marked interrupted before any workspace manager is created. The same SQLite transaction
appends a `state.changed` event with the previous phase and `cause=process_restart`, so the recovery
audit cannot disagree with the run row. Resume appends a `run.resumed` event with its
application-selected strategy and the number of incomplete tool calls closed without replay. A
verifier rejection similarly appends `repair.started` before builder work continues. See
[Fault-injection evidence](fault-injection.md) for the deterministic failure matrix.

## Rollback algorithm

For every touched path, TraceForge records the original state only once and updates the last-agent
hash after each mutation. Rollback compares the current file hash with that last-agent hash:

- equal and originally present: restore bytes and POSIX mode;
- equal and originally absent: remove the generated file and empty generated directories;
- different: report a conflict and preserve the user's later edit.

This is deliberately safer than `git reset`: it works in non-Git folders and never overwrites a
post-agent user change.

## HTTP and event surface

The same-origin service defaults to `127.0.0.1`. REST manages direct-task directories, projects,
provider references, connection probes, and run controls; it returns current state/diff/events and
resolves plan or action decisions. JSON and Markdown Proof Pack routes expose the evidence
projection. WebSocket pushes the persisted event stream. Origins are restricted to localhost, and
responses include CSP, frame denial, referrer, and MIME-sniffing headers. The React production
build is part of the Python wheel.

## Why one agent loop

Parallel agents would add impressive diagrams but weaken attribution, rollback ownership, and
debuggability for this project's scope. A separate verifier creates meaningful independence at the
decision boundary without concurrent writes. The result is small enough to explain line by line in
an interview and complete enough to demonstrate real failure recovery.

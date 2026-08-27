# Interview and demo guide

## 60-second design pitch

TraceForge is a local coding agent built without an agent framework. The model can propose tool
calls, but the application owns every consequential decision: a state machine, structured
clarification, plan approval, path and command policy, subprocess lifecycle, evidence freshness,
persistence, recovery, rollback, and independent verification. Simple, explicitly scoped work can
take a deterministic low-risk fast path; complex work still requires approval. The visual
workbench exposes the same persisted evidence used for recovery and a downloadable Proof Pack, so
the demo is not a decorative chat replay. The project's deliberate tradeoff is one reliable writer
plus one read-only verifier instead of many parallel agents.

## Two-minute video outline

| Time | Screen | Narration point |
| --- | --- | --- |
| 0:00-0:10 | Empty workbench and prefilled task | “A real multi-tenant cache leaks values.” |
| 0:10-0:25 | Clarification card, plan, and risk gate | Options prevent a silent compatibility assumption; the gate explains why approval is required. |
| 0:25-1:05 | Accelerated tool timeline and diff | Bounded native tools, dynamic plan status, real test command, stale-check invalidation. |
| 1:05-1:30 | Checks and verifier tabs | Exit code and output are evidence; verifier has no write/execute tools. |
| 1:30-1:50 | Evidence board and Proof Pack | Final verdict, persisted diff source, command-sandbox evidence, integrity digest, and conflict-aware recovery. |
| 1:50-2:00 | Architecture diagram | Model proposes; TraceForge controls and proves. |

## Likely questions

### Why not use LangChain or an Agents SDK?

The assignment requires ownership of the important logic. More importantly, the state machine,
tool-call/result ordering, evidence invalidation, and recovery semantics are the project's actual
value. Hiding them behind a framework would make the system harder to defend and test.

### Is the verifier really independent?

It receives a fresh verifier system prompt and evidence bundle, not the builder prompt. Its tool
surface is restricted to list/read/search plus structured verdict submission. It cannot mutate or
execute. It is process-level role separation, not a claim of statistical independence because the
configured model may be the same.

### How do you stop false completion?

`finish` is a tool request, not a trusted prose claim. It is rejected while any command-backed
check lacks fresh passing evidence. Mutations reset those checks. After `finish`, the verifier can
still reject the result and trigger a bounded repair cycle.

### Does automatic plan approval let the model bypass the user?

No. The model supplies structured facts, but deterministic application code assigns the gate.
Automatic approval requires one named file, small step/check counts, routine local checks, and no
clarification, explicit risk, or sensitive-area language. The plan stays visible. If the builder
later targets an undeclared path, execution pauses for a separate action approval before writing.

### What does the Proof Pack hash prove?

It fingerprints the persisted plan, gate, final diff, checks, verifier verdict, rollback state, and
event ledger, making accidental changes and two exported packs easy to compare. It is not a digital
signature or remote attestation; a local actor able to rewrite SQLite can recompute it. That precise
claim is more defensible than calling the artifact tamper-proof.

### Why not run everything in Docker?

The product targets a low-friction local demo on macOS/Linux. Routine commands use built-in
Seatbelt on macOS or Bubblewrap on Linux when its probe succeeds, so the common path gets real OS
enforcement without requiring a daemon or image build. The UI says `Policy only` when unavailable
and records a user-approved escape as `bypassed`. This is still not a claim that arbitrary hostile
native code is harmless; a disposable VM remains the correct outer boundary for that threat.

### Is approval the same as sandboxing?

No. Policy decides whether an action is routine, approval records a human decision, and the OS
sandbox constrains what a process can technically do. Planned checks normally stay sandboxed. An
unknown command pauses; approving it creates one unsandboxed invocation and a visible bypass event.
The header, tool row, and Proof Pack all expose the actual state, including degraded policy-only
machines.

### What happens on a crash in the middle of a command?

Startup changes active runs to `interrupted` and records the previous phase. Resume never blindly
replays a command. If the stored assistant message lacks a matching tool result, TraceForge inserts
a synthetic interruption result and asks the builder to inspect current state.

### Why SQLite and WebSocket events?

SQLite makes runs, snapshots, and event sequence durable without external services. Each event is
persisted before publication. A reconnect asks for events after its last sequence, making the UI a
recoverable view of the same source of truth rather than a best-effort stream.

### What would you build next?

First: evaluation across a fixed task corpus and a polished release/demo rehearsal. Second: richer
language-aware patch validation. Third: optional stronger Linux profiles and signed evidence.
Parallel writers and plugins come later because they complicate attribution and recovery more than
they improve this v0.1 demo.

## Code landmarks

- `src/traceforge/agent.py`: state machine and clarify/build/verify loop
- `src/traceforge/tools.py`: schemas, permission classification, process control
- `src/traceforge/sandbox.py`: OS backend probe, profiles, and explicit degraded/bypass metadata
- `src/traceforge/workspace.py`: path boundary, snapshots, diff, rollback
- `src/traceforge/storage.py`: WAL persistence and migrations
- `src/traceforge/api.py`: public data boundary and sequenced WebSocket
- `web/src/App.tsx`: evidence-oriented interaction surface
- `tests/test_demo.py` and `web/e2e/demo.spec.ts`: deterministic proof of the full story

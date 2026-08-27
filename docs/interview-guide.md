# Interview and demo guide

## 60-second design pitch

TraceForge is a local coding agent built without an agent framework. The model can propose tool
calls, but the application owns every consequential decision: a state machine, structured
clarification, plan approval, path and command policy, subprocess lifecycle, evidence freshness,
persistence, recovery, rollback, and independent verification. The visual workbench exposes the
same persisted event stream used for recovery, so the demo is not a decorative chat replay. The
project's deliberate tradeoff is one reliable writer plus one read-only verifier instead of many
parallel agents.

## Two-minute video outline

| Time | Screen | Narration point |
| --- | --- | --- |
| 0:00-0:10 | Empty workbench and prefilled task | “A real multi-tenant cache leaks values.” |
| 0:10-0:25 | Clarification card and plan | Options prevent a silent compatibility assumption; no file changed yet. |
| 0:25-1:05 | Accelerated tool timeline and diff | Bounded native tools, dynamic plan status, real test command, stale-check invalidation. |
| 1:05-1:30 | Checks and verifier tabs | Exit code and output are evidence; verifier has no write/execute tools. |
| 1:30-1:50 | Evidence board and rollback button | Final verdict, exact changed files, conflict-aware whole-run recovery. |
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

### Why not run everything in Docker?

The product targets a low-friction local demo on macOS/Linux. Application-level boundaries make
normal mistakes safer and the README clearly states they are not an OS sandbox. For hostile code,
an external container/VM is the correct additional boundary.

### What happens on a crash in the middle of a command?

Startup changes active runs to `interrupted` and records the previous phase. Resume never blindly
replays a command. If the stored assistant message lacks a matching tool result, TraceForge inserts
a synthetic interruption result and asks the builder to inspect current state.

### Why SQLite and WebSocket events?

SQLite makes runs, snapshots, and event sequence durable without external services. Each event is
persisted before publication. A reconnect asks for events after its last sequence, making the UI a
recoverable view of the same source of truth rather than a best-effort stream.

### What would you build next?

First: OS-level sandbox profiles and finer command capabilities. Second: richer language-aware
patch validation. Third: evaluation across a fixed task corpus. Parallel writers and plugins come
later because they complicate attribution and recovery more than they improve this v0.1 demo.

## Code landmarks

- `src/traceforge/agent.py`: state machine and clarify/build/verify loop
- `src/traceforge/tools.py`: schemas, permission classification, process control
- `src/traceforge/workspace.py`: path boundary, snapshots, diff, rollback
- `src/traceforge/storage.py`: WAL persistence and migrations
- `src/traceforge/api.py`: public data boundary and sequenced WebSocket
- `web/src/App.tsx`: evidence-oriented interaction surface
- `tests/test_demo.py` and `web/e2e/demo.spec.ts`: deterministic proof of the full story

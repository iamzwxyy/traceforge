# Interview and demo guide

## 60-second design pitch

TraceForge is a local coding agent built without an agent framework. The model can propose tool
calls, but the application owns every consequential decision: a state machine, structured
clarification, an automatic plan-review gate, path and command policy, subprocess lifecycle, evidence
freshness, persistence, recovery, rollback, and independent completion review. A separate per-turn
action-permission profile provides Manual, deterministic Automatic, and workspace-scoped Full
access without conflating approval with the OS sandbox. Explicit low-risk work continues after its
plan is recorded; complex, high-impact, clarified, or uncertain work pauses for review. Greetings and
read-only questions end as a distinct answer instead of fabricating a plan or proof. Per-turn
reasoning effort appears only when the exact endpoint/model supports it, without turning
provider-private reasoning into UI or evidence. The visual workbench keeps multi-turn conversation
primary while exposing the same persisted Trace used for recovery and
a downloadable Proof Pack. The project's deliberate tradeoff is one reliable writer plus one
read-only verifier instead of many parallel agents.

## Two-minute video outline

| Time | Screen | Narration point |
| --- | --- | --- |
| 0:00-0:10 | Empty workbench and prefilled task | “A real multi-tenant cache leaks values.” |
| 0:10-0:25 | Clarification card, plan, and risk gate | Options prevent a silent compatibility assumption; the gate explains why approval is required. |
| 0:25-1:05 | Accelerated tool timeline and diff | Bounded native tools, dynamic plan status, real test command, stale-check invalidation. |
| 1:05-1:30 | Checks and verifier tabs | Exit code and output are evidence; verifier has no write/execute tools. |
| 1:30-1:50 | Compact completion footer and Proof Pack | The answer stays primary; drill down to persisted diff source, command-sandbox evidence, integrity digest, and conflict-aware recovery. |
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

### Does automatic plan continuation let the model bypass the user?

No. The automatic path skips only the plan-review click for host-classified low-risk work. The model
still submits a structured scope and acceptance contract before any mutation, and the application
still enforces workspace paths, undeclared-file approval, command classification, sandboxing, and
destructive-operation denial. Each action-permission profile keeps the same meaning under either
plan-gate outcome. A direct answer executes nothing and is labeled separately; completion review is
also separate and read-only.

### What does the Proof Pack hash prove?

The v2 artifact hash covers the complete public frozen JSON for one successful turn; separate
semantic, event-chain, and Diff hashes make individual layers easy to compare. The success state,
closed turn, terminal events, and artifact are committed atomically, so there is no public
half-success without evidence. None of these hashes is a digital signature or remote attestation;
a local actor able to rewrite SQLite can recompute them. That precise claim is more defensible than
calling the artifact tamper-proof.

### Why not run everything in Docker?

The product targets a low-friction local demo on macOS/Linux. Routine commands use built-in
Seatbelt on macOS or Bubblewrap on Linux when its probe succeeds, so the common path gets real OS
enforcement without requiring a daemon or image build. The UI says `Policy only` when unavailable
and records a user-approved escape as `bypassed`. This is still not a claim that arbitrary hostile
native code is harmless; a disposable VM remains the correct outer boundary for that threat.

### Is approval the same as sandboxing?

No. Policy decides whether an action is routine, approval records a human decision, and the OS
sandbox constrains what a process can technically do. In the default Automatic profile, planned
checks stay sandboxed and an unknown command pauses; approving it creates one unsandboxed
invocation and a visible bypass event. Manual confirmations stay sandboxed, and workspace Full
access never creates an automatic bypass.
The header, tool row, and Proof Pack all expose the actual state, including degraded policy-only
machines.

### What do the three action-permission choices actually mean?

Manual is “ask every edit or command,” while reads remain uninterrupted. Automatic is the default
deterministic policy: planned edits/checks and known reads run, drift and unknown commands ask.
Workspace Full access removes those soft prompts only inside the Workspace guard and an enforced OS
sandbox; without enforcement, unknown code falls back to a human decision. It is deliberately not
host-wide Codex `danger-full-access`, never disables hard destructive denials, and is not persisted
as the next turn's default. Tool events record the base and effective decision plus actual bypass,
so the labels can be audited rather than trusted.

### What happens on a crash in the middle of a command?

Startup changes active runs to `interrupted` and records the previous phase. Human replies are
request-bound SQLite receipts, so a pending card can reopen and an accepted reply can be consumed
after explicit resume without matching a later prompt. For an approved action, receipt consumption,
the execution state, and `tool.started` are one transaction committed before external execution.
If that marker has no matching completion after restart, TraceForge marks the action uncertain,
never replays it, inserts any required protocol closure, and asks the builder to inspect current
state. Clarification and plan replies are paired with the exact latest unanswered source call,
even if a provider reused an older call ID; rejecting an action atomically preserves the exact
failed tool result instead of relying on later generic repair.

### What happens if rollback crashes or races Resume?

Both operations use the same per-run lifecycle lock, so either Resume registers its worker first
and rollback conflicts, or rollback wins and Resume sees the terminal state. Rollback abandons any
live human decision before touching files. Each file recognizes both its agent-written state and
its already-restored target state, which makes a retry converge after a mid-batch failure while
still preserving a later user edit as a conflict. Only after the files converge does one SQLite
transaction publish `rolled_back` plus the complete result; it does not pretend the filesystem and
database are one transaction.

### How does reasoning effort work, and do you expose chain of thought?

It is an orthogonal per-turn control: the automatic gate decides whether the plan pauses, action
permission decides which tools ask, the sandbox limits commands, and reasoning effort only selects
the model protocol level. TraceForge advertises levels from an exact official endpoint/model
catalog; `auto` omits the field and unknown routes stay default-only. One selection is frozen across
planner, builder, repairs, and verifier. DeepSeek's required `reasoning_content` is replayed only as
private protocol state, then scrubbed on terminal paths; UI, events, verifier evidence, and Proof
Pack show the requested level but never the hidden reasoning text.

### Why SQLite and WebSocket events?

SQLite makes runs, snapshots, and event sequence durable without external services. Each event is
persisted before publication. A reconnect asks for events after its last sequence, making the UI a
recoverable view of the same source of truth rather than a best-effort stream.

### What would you build next?

The fixed quality corpus and two low-frequency real-model scenarios now cover the main claims.
Next: richer language-aware patch validation. After that: optional stronger Linux profiles
and signed evidence.
Parallel writers and plugins come later because they complicate attribution and recovery more than
they improve this v0.1 demo.

## Code landmarks

- `src/traceforge/agent.py`: state machine and clarify/build/verify loop
- `src/traceforge/tools.py`: schemas, permission classification, process control
- `src/traceforge/sandbox.py`: OS backend probe, profiles, and explicit degraded/bypass metadata
- `src/traceforge/workspace.py`: path boundary, snapshots, diff, rollback
- `src/traceforge/storage.py`: WAL persistence and migrations
- `src/traceforge/api.py`: public data boundary and sequenced WebSocket
- `web/src/App.tsx`: conversation-first interaction surface with on-demand evidence
- `tests/test_demo.py` and `web/e2e/demo.spec.ts`: deterministic proof of the full story
- `scripts/evaluate_real_model.py`: opt-in provider acceptance with hidden semantic checks

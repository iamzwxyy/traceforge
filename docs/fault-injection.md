# Fault-injection evidence

TraceForge treats recovery as a product behavior, not a promise made by the model. The deterministic
tests below inject failures at the boundaries most likely to matter during a live demonstration.

| Injected failure | Required invariant | Persisted evidence | Automated coverage |
| --- | --- | --- | --- |
| Process disappears during an unfinished run | Startup changes the run to `interrupted` without guessing that it completed | `state.changed` records the previous state and `cause=process_restart` | `test_mark_active_runs_interrupted` and `test_mark_all_active_runs_interrupted` |
| Model connection, timeout, rate limit, or temporary server failure persists across bounded retries | Preserve the workspace and history in `interrupted`; allow connection settings to change and resume without starting over | `model.retry` records every application-owned retry; recoverable `error`, `state.changed`, and `run.resumed` record the pause and continuation | `test_transient_model_outage_pauses_and_resumes_without_losing_run` and `test_api_allows_provider_repair_then_resume_after_transient_outage` |
| Provider violates its response contract or throws an unexpected exception | End with an explicit failure and completion event; never leave a ghost run stuck in an active phase | `error`, terminal `state.changed`, and `run.completed` | `test_unexpected_provider_exception_never_leaves_a_ghost_run` |
| Provider history ends with an unmatched tool call | Resume closes the protocol gap with a synthetic failure and never replays the unknown side effect | `run.resumed` records the prior phase, selected strategy, and repaired call count | `test_resume_closes_an_incomplete_tool_call_without_replaying_it` |
| Independent verifier rejects a result after a passing check | The repair edit invalidates the old check; `finish` stays blocked until the exact check passes again | `repair.started`, a pending check update, and two distinct command result events | `test_repair_cannot_reuse_a_passing_check_from_before_the_edit` |
| User edits one of several agent-touched files before rollback | Preserve the conflicting file while safely restoring every unchanged agent version | `rollback.completed` separates `restored`, `removed`, and `conflicts` | `test_rollback_restores_safe_files_while_preserving_one_conflict` |

TraceForge disables the OpenAI SDK's hidden retries, applies a bounded per-attempt timeout, and
records its own retry decisions. Exhausting a retryable failure pauses rather than fails the run;
an interrupted run has no live worker, so the user may repair provider settings and then resume.

`run.resumed` is emitted before resumed work continues. Its strategy is application-selected:
clarification and plan approvals return to their human gate, a persisted low-risk decision resumes
the fast path, and previously approved execution resumes only after an explicit inspect-first
instruction. `repair.started` records the bounded cycle number and the verifier finding that caused
it. Both events enter the same append-only sequence hashed by the Proof Pack.

These tests intentionally avoid timing-based assertions. They inspect SQLite state, exact event
payloads, model-visible protocol repair, file contents, and command-event counts, so failures are
reproducible without an API key. Event-broker pressure is also adversarially covered: a slow
subscriber may lose bounded in-memory queue entries, but cannot fail execution, and WebSocket
sequence gaps are replayed from the authoritative SQLite ledger.

# Fault-injection evidence

TraceForge treats recovery as a product behavior, not a promise made by the model. The deterministic
tests below inject failures at the boundaries most likely to matter during a live demonstration.

| Injected failure | Required invariant | Persisted evidence | Automated coverage |
| --- | --- | --- | --- |
| Process disappears during an unfinished run | Startup changes the run to `interrupted` without guessing that it completed | `state.changed` records the previous state and `cause=process_restart` | `test_mark_active_runs_interrupted` and `test_mark_all_active_runs_interrupted` |
| Provider history ends with an unmatched tool call | Resume closes the protocol gap with a synthetic failure and never replays the unknown side effect | `run.resumed` records the prior phase, selected strategy, and repaired call count | `test_resume_closes_an_incomplete_tool_call_without_replaying_it` |
| Independent verifier rejects a result after a passing check | The repair edit invalidates the old check; `finish` stays blocked until the exact check passes again | `repair.started`, a pending check update, and two distinct command result events | `test_repair_cannot_reuse_a_passing_check_from_before_the_edit` |
| User edits one of several agent-touched files before rollback | Preserve the conflicting file while safely restoring every unchanged agent version | `rollback.completed` separates `restored`, `removed`, and `conflicts` | `test_rollback_restores_safe_files_while_preserving_one_conflict` |

`run.resumed` is emitted before resumed work continues. Its strategy is application-selected:
clarification and plan approvals return to their human gate, a persisted low-risk decision resumes
the fast path, and previously approved execution resumes only after an explicit inspect-first
instruction. `repair.started` records the bounded cycle number and the verifier finding that caused
it. Both events enter the same append-only sequence hashed by the Proof Pack.

These tests intentionally avoid timing-based assertions. They inspect SQLite state, exact event
payloads, model-visible protocol repair, file contents, and command-event counts, so failures are
reproducible without an API key.

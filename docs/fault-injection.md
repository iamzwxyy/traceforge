# Fault-injection evidence

TraceForge treats recovery as a product behavior, not a promise made by the model. The deterministic
tests below inject failures at the boundaries most likely to matter during a live demonstration.

| Injected failure | Required invariant | Persisted evidence | Automated coverage |
| --- | --- | --- | --- |
| Process disappears during an unfinished run | Startup changes the run to `interrupted` without guessing that it completed | `state.changed` records the previous state and `cause=process_restart` | `test_mark_active_runs_interrupted` and `test_mark_all_active_runs_interrupted` |
| Model connection, timeout, rate limit, or temporary server failure persists across bounded retries | Preserve the workspace and history in `interrupted`; allow connection settings to change and resume without starting over | `model.retry` records every application-owned retry; recoverable `error`, `state.changed`, and `run.resumed` record the pause and continuation | `test_transient_model_outage_pauses_and_resumes_without_losing_run` and `test_api_allows_provider_repair_then_resume_after_transient_outage` |
| An interrupted turn with explicit reasoning effort is pointed at a route that does not support it | Keep the run `interrupted`, preserve the frozen turn setting, and reject resume before any model request instead of silently downgrading | Existing turn and `reasoning_effort` remain unchanged; no false `run.resumed` event or provider call is created | `test_resume_preserves_effort_and_blocks_an_incompatible_new_route` |
| An external SQLite reader prevents private-reasoning WAL truncation | Bound the cleanup wait, keep the scrubbed row recoverable in `interrupted`, block further model calls while cleanup is pending, and allow Stop to retry after the reader closes | `error` and `state.changed` record `cause=provider_reasoning_cleanup_pending`; the internal pending flag survives until a verified checkpoint | `test_secure_checkpoint_rejects_busy_wal_then_succeeds_after_reader_closes` and `test_cleanup_checkpoint_failure_interrupts_worker_without_a_ghost_run` |
| Provider violates its response contract or throws an unexpected exception | End with an explicit failure and completion event; never leave a ghost run stuck in an active phase | `error`, terminal `state.changed`, and `run.completed` | `test_unexpected_provider_exception_never_leaves_a_ghost_run` |
| Planner emits prose before correcting itself to a terminal tool, or a builder/verifier response carries rejected or terminal prose | Publish only progress backed by accepted non-terminal tools and one canonical terminal result; never show the same answer twice | Invalid/terminal drafts stay in internal protocol history and create no public `message`; `turn.completed` is authoritative | `test_planner_prose_correction_publishes_only_the_canonical_answer`, `test_planner_read_progress_remains_visible_before_the_answer`, `test_terminal_builder_and_verifier_drafts_are_not_published`, and `test_builder_publishes_only_progress_backed_by_accepted_tools` |
| Provider history ends with an unmatched tool call | Resume closes the protocol gap with a synthetic failure and never replays the unknown side effect | `run.resumed` records the prior phase, selected strategy, and repaired call count | `test_resume_closes_an_incomplete_tool_call_without_replaying_it` |
| Process stops or user cancels while an action approval is open | Clear the persisted approval ID, record abandonment, and never let a stale decision authorize a later action | `approval.resolved` records `outcome=abandoned` plus `process_shutdown` or `user_cancelled` | `test_restart_abandons_pending_approval_without_replaying_action` and `test_cancel_abandons_pending_approval_and_stale_id_cannot_resolve_it` |
| Independent verifier rejects a result after a passing check | The repair edit invalidates the old check; `finish` stays blocked until the exact check passes again | `repair.started`, a pending check update, and two distinct command result events | `test_repair_cannot_reuse_a_passing_check_from_before_the_edit` |
| A multi-file native patch writes its first file and the second write raises | Report and persist only the file that actually changed, even when repeated failures terminate the turn | failed `tool.completed` metadata, `diff.updated`, and terminal `turn.completed.changed_files` all retain the partial mutation | `test_mutation_metadata_reports_only_files_that_actually_changed` and `test_partial_edit_files_survive_terminal_builder_failure` |
| User edits one of several agent-touched files before rollback | Preserve the conflicting file while safely restoring every unchanged agent version | `rollback.completed` separates `restored`, `removed`, and `conflicts` | `test_rollback_restores_safe_files_while_preserving_one_conflict` |
| Run A's response or event is delayed across a switch to B, or B's Diff remains pending | Render and act only on B; never leak A's error, local loading state, Diff, event, Proof, or destructive control target beneath B's selection | Visible run derives from selection; actions, errors, request generations, inbound event owner, WebSocket Diff epoch, and Proof `run_id` agree before use | Five `artifact-isolation.spec.ts` scenarios wait for response completion plus browser commits and inject a stale socket without timing guesses |

TraceForge disables the OpenAI SDK's hidden retries, applies a bounded per-attempt timeout, and
records its own retry decisions. Exhausting a retryable failure pauses rather than fails the run;
an interrupted run has no live worker, so the user may repair provider settings and then resume.
Resume first validates the active turn's frozen reasoning effort against the newly selected exact
route/model capability. Provider repair may change the endpoint or model, but it cannot silently
change the semantic request made by the interrupted turn.

`run.resumed` is emitted before resumed work continues. Its strategy is application-selected:
clarification and Plan-mode approvals return to their human gate, a persisted Agent auto-continue
decision resumes without inventing a click, and previously approved execution resumes only after an explicit inspect-first
instruction. Pending action approvals are different: they are abandoned before interruption/resume
can proceed because the process cannot prove that the exact call is still current. `repair.started` records the bounded cycle number and the verifier finding that caused
it. Both events enter the same append-only sequence hashed by the Proof Pack.

These tests primarily inspect SQLite state, exact event
payloads, model-visible protocol repair, file contents, and command-event counts, so failures are
reproducible without an API key. The WAL-busy case additionally uses a generous upper bound to
catch accidental reintroduction of SQLite's multi-second lock wait. Event-broker pressure is also adversarially covered: a slow
subscriber may lose bounded in-memory queue entries, but cannot fail execution, and WebSocket
sequence gaps are replayed from the authoritative SQLite ledger.

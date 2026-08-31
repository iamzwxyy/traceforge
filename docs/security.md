# Security model

TraceForge reduces accidental damage from model-proposed actions with three separate layers:
application policy, explicit approval, and an optional OS-enforced command sandbox. It does not
claim that those layers make arbitrary hostile code harmless. The user account running TraceForge
remains the ultimate operating-system authority, and the UI reports when OS enforcement is absent
or deliberately bypassed.

## Trust boundaries

- Model text and tool arguments are untrusted input.
- The selected workspace is the only file scope.
- Commands may execute project code and are therefore higher risk than reads.
- The browser UI is local, but REST/WebSocket input is still validated.
- API credentials are configuration, never task context.
- Provider-private reasoning is protocol state, never user-visible evidence or a permission signal.

## File boundary

Every path must be a non-empty relative path. Before a read or write, TraceForge resolves the real
ancestor and checks it remains below the fixed workspace root and, when selected, the active
project virtual root. Absolute paths, traversal, `.git`
components, non-regular file targets, existing targets for `create_file`, and writes through any
symlink component are rejected.

Reads omit `.env` and `.env.*` except `.env.example`; directory listing and search also hide these
files. This reduces accidental disclosure to the model but is not a general secret vault.

## Workspace instruction boundary

Project guidance is untrusted context, not policy. TraceForge v1 recognizes only an exact
`AGENTS.md` directly inside the selected workspace root. It does not walk the home directory,
parents, nested directories, include references, skills, plugins, or environment files. The loader
uses non-following metadata/open checks, accepts only a stable regular UTF-8 file, rejects NUL and
credential-like content, and limits the complete model-facing framed message to 32 KiB. A changed,
replaced, symlinked, oversized, or unsafe source fails closed before the new turn is committed. A
root with more than 10,000 direct entries is rejected independently of enumeration order. Even
below 32 KiB, the protected rules, direct request, and Planner schemas must fit the active model's
configured context window before the turn is created.

The private content is stored in the owner-only SQLite database for deterministic model requests
and recovery. The dedicated snapshot/manifest path does not copy it into `RunRecord`, events,
REST/WebSocket payloads, Proof Packs, errors, or the React rule view. Public rule surfaces receive a
validated manifest containing only the relative root path, scope, byte count, capture time, and
SHA-256 values; the browser parser rebuilds that narrow shape and discards extra fields. The UI says
“loaded”, never “enforced”. This is provenance minimization, not secrecy: the original text is sent
to the configured model provider, and model-authored answers, plans, or tool arguments may quote it
into normal run evidence. Do not put secrets in `AGENTS.md`; the loader's credential-like check is
limited to the active provider key and recognizable `sk-...` shapes.

Provider credentials can change while a turn is interrupted. Therefore capture-time inspection is
not sufficient: before every model call, TraceForge rechecks the complete messages and tool schemas,
including the stored rule snapshot, against the credential active for that provider. The agent
checks both leaf values and compact JSON assembly, and the OpenAI-compatible provider repeats the
check on its final request kwargs before the HTTP client can serialize them. A formerly ordinary
string—or a value synthesized only across JSON token boundaries—that matches the current API key
blocks transmission; the immutable snapshot is neither rewritten nor silently redacted.
Because the OpenAI SDK may reorder typed request fields, an async request hook performs the final
check again on the exact UTF-8 body after SDK transformation but before the httpx transport runs.

The synchronous resume preflight applies the same leaf, framed-JSON, and compact-JSON checks to the
full saved `RunRecord`, immutable event ledger, pending or accepted durable decision, and each plausible model request
after the sealed guidance message has been inserted. This catches a credential assembled only at
the guidance/history boundary before an async task exists. If a newly rotated credential collides
with that formerly ordinary context, resume performs no provider or tool call and leaves the run
interrupted. Restoring the prior credential leaves the exact turn resumable. Explicit cancellation instead enters a narrow
destructive transaction that never serializes the unsafe row: it abandons the active decision,
clears its payload plus model-facing messages/subjects, replaces the conversation projection with a
neutral cancelled turn, and releases the workspace while preserving workspace files and the sealed
instruction snapshot. Existing immutable events and Proof Packs are not rewritten. The transaction
sets a physical-cleanup marker before attempting a WAL checkpoint; a busy reader can delay byte
reclamation but cannot leave the run active or replay an accepted action. Recovery does not copy an
unsafe old stream payload, and it chooses one timestamp that is safe against every credential guard
before using that timestamp consistently for the neutral run, decision, turn, and terminal events.

The frozen boundary applies to automatic discovery and injection. `AGENTS.md` remains an ordinary
workspace file that read/edit tools can inspect, as can nested files with the same name. Later disk
content is project data, not a replacement for the marked turn snapshot; the model-facing framing
makes that distinction explicit.

Each new turn atomically commits an immutable snapshot, including an explicit empty one. Follow-ups
capture current disk state; resume reuses the interrupted turn's stored snapshot and is rejected for
legacy turns that lack one. Before any native edit or command side effect, `ToolRegistry` requires
the active turn's persisted snapshot hash to match its manager binding. This prevents an old
approval or a partially initialized turn from executing under unknown project guidance.
The upgrade migration abandons pending and accepted clarification, plan, and action receipts that
predate snapshots, clears replay state, and leaves the interrupted run available for an explicit
stop before follow-up; old decision endpoints cannot accept those abandoned IDs.

The model receives project prose in a separate user-role message below the fixed system contract
and ahead of the current direct request. Its safety framing states that project text cannot grant
permissions, suppress approvals, weaken the OS sandbox or credential controls, expose secrets, or
authorize paths outside the selected workspace. The current user request wins over conflicting
project defaults according to the model-facing contract, but TraceForge cannot mechanically prove
that the model resolves every semantic conflict correctly. Independent local permission and path
checks—not model obedience—enforce the no-expansion security boundary.

## Plan and mutation policy

Plan mode is an interaction choice, not a permission level. In the default Agent mode, the model
still submits a structured plan before mutation, but the application records its risk assessment
and proceeds without a plan-approval click. In Plan mode, every valid executable plan pauses for
human review, even when it names one routine file. Conversational and explicitly read-only requests
can instead end in the distinct `answered` state in either mode; that state cannot claim completion
evidence or expose a Proof Pack. The canonical Markdown document and policy reasons remain visible
for executable work in both modes. Selecting Agent mode cannot turn a denied command into an
allowed command or expand the workspace boundary.

After planning, each `create_file` and every file in an `apply_patch` is compared with the declared
scope. The base policy marks an unexpected path as `ask` before any bytes are written; the selected
action-permission profile decides whether that soft decision pauses or is automatically handled.
The Workspace real-path guard remains mandatory in every profile. Scope classification answers
“was this planned?” while the path guard and command sandbox answer “what can this action
technically reach?”

For user-facing per-turn attribution, those two native edit tools fingerprint canonical candidate
paths before and after execution. Only actual changes are recorded, including a successful first
write when a later file fails. The claim is intentionally limited: approved project commands can
also mutate the workspace, and TraceForge does not yet perform a whole-tree command audit.

Completion review is a third, separate concept. After fresh checks, the verifier receives evidence
through a read-only tool surface and cannot mutate files or execute commands. Its pass/fail decision
does not grant runtime permissions; a rejection only returns bounded findings to the builder.

Every request the host classifies as workspace-dependent crosses a host-owned project-target
boundary before the model can inspect or act. `RequestResolution` makes target routing auditable: it
records the work kind, workspace dependency, target reference and status, target-routing ambiguity,
the overview-evidence requirement, and bounded reasons. Model-authored requirements questions carry
their target/scope/constraint/acceptance dimension separately, and accepted answers are stored as
resolved clarifications. Neither structure is an authorization token.

The resolver operates on a canonical semantic view that masks fenced/inline snippets and quoted
content literals before action and project-target extraction. It composes requested effect, local
object evidence, verified target roles, polarity, and cardinality instead of accepting an isolated
keyword as authority. General explanations of artifact names remain conversation unless a local
read/write relation governs the artifact. Unknown software result-state wording is classified as
`undetermined`: inspection and clarification remain available, but a resulting plan always requires
explicit review and cannot be auto-approved. Its result-state grammar includes causative imperatives,
obligation modals, desired-result frames, and Chinese disposal constructions; question and
epistemic-complement guards keep explanations, learning, and advice outside executable routing.

Candidate project roots come from bounded manifest-name discovery, not model prose; hidden/temp/
cache/build directories and symlinks are excluded. Candidate device/inode/ctime identities are
captured before a picker is shown. An explicit or reliable adjacent reference resolves directly;
an unspecified single-target request with multiple possible roots requires a host-authored choice;
explicit alternatives produce a picker limited to those verified candidates; an unsupported joint
multiple-target request fails closed instead of silently narrowing the user's stated scope. The resulting `ProjectTarget`
is persisted atomically with decision consumption. Moving, replacing, or deleting it invalidates
the target rather than authorizing a surviving sibling.

The chosen target becomes the virtual root for reads, writes, and commands. Each scoped read opens
a temporary root descriptor, checks it against the persisted identity, and traverses every
component relative to that descriptor with symlink following disabled; scoped search uses the same
descriptor walker rather than an external pathname-based process. Native writes resolve from the
same target, and project commands use that root as their bounded working directory. Scoped commands
require an enforced OS sandbox: Seatbelt denies sibling-project file reads and writes, while
Bubblewrap hides the containing workspace and mounts only the selected target back. These controls
reject ordinary parent traversal, sibling-project access, and symlink aliases. Descriptor-relative
reads additionally discard buffered content after a target identity change. Native writes and the
command launcher still perform some final operations through validated pathnames rather than one
held directory descriptor; a hostile concurrent rename between validation and that operation is a
known residual TOCTOU boundary, not a claimed guarantee.

Project selection is deliberately not an action approval. It cannot promote Manual to Automatic,
disable a plan gate, turn a denied command into an allowed command, bypass the real-path guard, or
weaken the OS sandbox. Requirements clarification is also separate: project choice does not consume
its two-round budget. The Planner contract allows only unresolved material target, scope,
constraint, or acceptance choices to use that budget. The host enforces declared dimension,
material effect, rationale, stable semantic keys, option/round limits, and durable answers; it does
not claim to prove the natural-language necessity of every model-authored question. The project-root
target namespace itself is host-owned, so a provider cannot use requirements clarification to ask
for or override that target on explicit, automatic, or conversation routes.

Effect ceilings are also host-owned. Conversation routes cannot access workspace tools or submit a
plan. Read routes may inspect but every `submit_plan` is rejected. Executable routes retain the
normal plan, permission, path, and sandbox gates. `undetermined` routes may inspect and propose a
plan only through forced explicit plan review with a minimum medium-risk classification.

`.git` path components and `.env`-family basenames are compared case-insensitively before any
descriptor traversal, so aliases such as `.GIT` and `.ENV.prod` cannot bypass the boundary on the
default case-insensitive macOS filesystem (and are rejected consistently on Linux).

Scoped list/read/search form a bounded resource boundary inside that project target. They run
outside the API event loop and use streaming directory iteration or unbuffered descriptor reads.
Listing caps entries and rows;
file reads cap requested lines, individual line size, scanned file bytes, and UTF-8 persisted
output. Search additionally caps regular-expression length, per-match time, one total wall-clock
deadline, files visited, total scanned bytes, and returned text. Invalid or timed-out expressions
become ordinary failed tool results; oversized/incomplete walks are marked without persisting
over-budget content.

The clarification regression boundary is a fixed Chinese/English matrix. Each case asserts the
expected host route and target for unspecified, explicit, inherited, workspace, multiple-target,
alternative, local-artifact, literal/snippet, explanatory, negated, result-state, executable,
greeting, and general-knowledge requests. Agent tests separately check that a required picker
suppresses provider calls, that the host owns project-root questions, that uncertain effects force
review, and that requirement semantic keys and answers remain bound across decision and follow-up
paths. This is a bounded regression contract,
not a global natural-language precision/recall or four-dimension accuracy measurement.

## Per-turn action-permission profiles

Every turn freezes one `ApprovalMode` in SQLite and in its immutable conversation-turn snapshot.
Resume and repair read that persisted value; they never consult a later browser preference. The
profiles modify only soft `allow`/`ask` outcomes after the invariant policy has run:

| Profile | Native edits | Known/planned commands | Scope drift / unknown command |
| --- | --- | --- | --- |
| `manual` | ask each time | ask each time with `bypass=False` | ask each time with `bypass=False` |
| `automatic` (default) | planned edits allow | allow + sandbox | ask; an approved unknown command keeps the legacy one-shot bypass |
| `full_access` (workspace-scoped) | allow inside the Workspace guard | allow + sandbox | native drift allows; unknown command auto-allows only under an enforced OS sandbox |

`full_access` is intentionally not host-wide Codex `danger-full-access`. If no OS backend is
enforced, an unknown/code-executing command falls back to a human approval instead of silently
running with host authority. The UI calls this **完全访问（工作区）**, shows it throughout the turn,
and does not persist it as the next turn's default. `sudo`, disk/power tools, destructive Git,
recursive forced deletion, explicit outside-workspace paths, native-write traversal/symlinks, and
credential exposure remain denied in all profiles.

Each tool completion records the base decision, effective decision, profile, authorization source,
outcome, reason, and actual sandbox-bypass fact. A pending approval also snapshots those fields so
the approval card can say whether clicking once will retain or bypass the OS sandbox. Cancellation
marks an unconsumed approval `abandoned`. Restart instead preserves its exact request-bound receipt:
explicit resume reopens a pending card or consumes an already accepted response; it never lets that
ID authorize a reconstructed or later action.

## Durable decision boundary

Every clarification, plan review, and action approval is bound to a random request ID and a hash of
the exact subject shown to the user. The backend commits the canonical response to a SQLite inbox
before acknowledging it. An exact retry is safe even after consumption; a different payload under
the same ID, the wrong decision kind, an abandoned request, or an old ID is rejected. The browser
also uses synchronous single-flight guards and keeps a submitted card locked until the run changes,
but those controls are usability defenses—the database compare-and-set remains authoritative.
Clarification schemas reject duplicate question IDs, duplicate answers, and responses that mix or
omit option/custom values. Restart repair pairs an accepted reply with the exact latest unanswered
source call rather than trusting a provider call ID to be globally unique.
Host-authored project selection remains `purpose=project_scope` and does not increment the persisted
requirements round; only `purpose=requirements` decisions count toward the two-round limit.

Approved actions use an at-most-once crash boundary. The transaction that consumes approval also
clears the pending card, enters execution, and persists `tool.started`; only then may the external
tool run. A crash before that transaction leaves an accepted receipt that explicit resume can
consume. A crash after it but before `tool.completed` is inherently unable to prove whether the
side effect occurred, so TraceForge marks the receipt `uncertain`, never auto-replays it, and asks
the resumed builder to inspect current state. This favors duplicate-side-effect prevention over
blind at-least-once execution. A rejection is different: its deterministic failed tool result and
completion evidence commit with receipt consumption, preserving the user's exact decision across a
crash.

## Command policy

Commands are argv arrays passed to `asyncio.create_subprocess_exec`; shell parsing and expansion do
not occur.

| Base class | Automatic-mode decision | Examples |
| --- | --- | --- |
| Approved acceptance check | allow + sandbox | exact argv recorded in the approved plan |
| Focused variant of an approved Pytest check | allow + sandbox, diagnostic only | same launcher family with a narrower selector or safe display/filter flags |
| Known read-only | allow + sandbox | `git status`, `git diff`, `rg`, `ls`, `cat` |
| Unknown / code execution | ask once; approval is a one-shot sandbox bypass | Python scripts, installers, network clients |
| Destructive / privileged | deny | `sudo`, `git reset --hard`, recursive forced `rm` |

An approval card shows the exact arguments, reason, risk, selected profile, and whether approval
retains or bypasses the sandbox. Rejection returns a persisted failed tool result so the builder
can choose a safer path. In Automatic mode, approving a base-policy unknown command bypasses the OS
sandbox for that invocation only; Manual-mode confirmations stay sandboxed. The resulting tool
event, timeline badge, and Proof Pack record the actual fact. Commands default to 120 seconds, are capped at 600 seconds, and run in a separate
process group so Stop/timeout can terminate descendants.

Routine variants are deliberately narrow: `python` and `python3` Pytest invocations are one family,
while output-writing flags, interactive debugging, changing to `uv`, another check tool, an
installer, a network client, or arbitrary `python -c` still require approval. A focused variant may
help diagnose a failure, but it never marks a broader planned command as passed; `finish` still
requires fresh evidence from every exact approved argv.

## OS command sandbox

Every command gets a private temporary home, temporary directory, and cache directory. TraceForge
then probes and selects one backend:

| Platform | Enforced backend | Main boundary |
| --- | --- | --- |
| macOS | built-in Seatbelt (`sandbox-exec`) | writes limited to the command root and temp; a selected project also hides sibling file contents; external network denied; loopback retained |
| Linux | working non-setuid Bubblewrap | read-only host root, writable command root/temp, read-only `.git`, separate user/PID/network namespaces; a selected project masks its containing workspace before remounting only that project |
| Unsupported or failed probe | none (`policy_only`) | application path/argv policy and approval only for workspace-root commands; selected-project commands fail closed |

Both enforced profiles block configured credential files, common credential locations, and real
`.env*` files other than `.env.example`. Seatbelt additionally denies ordinary file-content reads
under the user's home except the selected workspace and runtime paths. Bubblewrap keeps the host
root readable for tool compatibility but masks the enumerated sensitive paths. These are accurate,
different claims; the UI does not collapse them into a generic “secure” label.

When a project below the workspace root is selected, its command boundary cannot be bypassed by an
approval: an unavailable backend rejects the command, Seatbelt denies reads from the rest of the
workspace, and Bubblewrap removes the rest of that workspace from the command namespace. Absolute
executables inside the workspace but outside the selected project, including target-local symlinks
to such executables, are rejected before launch.

The sandbox is constructed by TraceForge, not inferred from an approval. Backend, enforcement
status, network mode, and any bypass are stored with each command result. The Proof Pack aggregates
those records as `enforced`, `mixed`, `bypassed`, `policy_only`, or `not_used`. See
[Sandbox design and adversarial evidence](sandbox.md).

## Credentials and redaction

The real provider reads a credential from `OPENAI_API_KEY`, a user-selected local file, or the
owner-only managed file created by direct API-key entry. Direct entry is accepted as a redacted
request field, atomically written with `0600` permissions, and never placed in SQLite. Validation
errors omit request inputs so a rejected key cannot be reflected in a 422 response. Every file
reference must resolve to a regular file smaller than 16 KiB; on POSIX it must be owner-only, and
its content must be exactly one non-empty line containing 12–16,383 UTF-8 bytes. Credentials that
collide with unavoidable fields or literals in the credential-conflict recovery protocol are
rejected at configuration/manager construction rather than accepted into a state that could not be
cancelled safely. Public status/configuration responses expose the
source and readiness flag, never the value. A draft connection check resolves the key in memory and
verifies native tool calling rather than only endpoint reachability. In test-and-save, only success
creates a new managed credential file and swaps the configuration row; a failed draft leaves neither
its key nor its route in persistent state. The explicit save-only path may retain a draft for later
testing but always clears the verification timestamp. New runs,
follow-ups, and resumes are gated on both a readable credential and a successful probe of the saved
configuration; a failed recheck of that same saved configuration clears readiness.

New managed keys live beneath a dedicated `0700` directory opened without following symbolic
links; files are created relative to that verified directory descriptor with exclusive `0600`
creation. Cleanup recognizes only that directory and the exact legacy managed filename, so a
user-supplied credential with a similar name is never deleted as application-owned state.

Before any model-proposed child command or local file-manager launcher starts, TraceForge removes environment variables whose
names indicate keys, passwords, passphrases, secrets, tokens, or credentials, as well as common
agent socket variables. Normal variables such as `PATH` remain available. Model and tool text are
also redacted for the configured credential and token-shaped `sk-...` values before public events
are emitted. The CI scanner examines Git-tracked UTF-8 files for private-key headers and common
credential literals.

Streaming does not expose raw provider chunks. The public path incrementally decodes only the
schema-declared string in `respond_to_user.content` or `finish.summary`; ordinary model content,
other tool arguments, and private reasoning are excluded. A stateful redactor withholds every
suffix that could still grow into the configured credential or an `sk-...` token, then persists
only stable redacted deltas. The completed record is checked to extend the already-published prefix
and to equal the validated tool argument. Malformed JSON, unsupported finish reasons, output after
a terminal chunk, size-limit violations, missing terminal metadata, retry, or cancellation abort the
attempt under its own stream ID. Token matching is boundary-independent and happens before exact-key
replacement, so adjacent credentials and token values ending in `-` cannot create a newly exposed
prefix. The replacement glyph is selected outside the configured key's alphabet, so even a key equal
to a legacy marker—or text on either side of a removed interval—cannot synthesize the key again.
Every provider response then crosses one canonical ingress before semantic use. Presentation-only
fields (`respond_to_user.content`, `finish.summary`, and ordinary progress text) are recursively
redacted. Provider-controlled identities, private reasoning, and every non-terminal tool argument
are semantic control data: credential-bearing values are rejected before history, events, approval,
or execution instead of being mutated and then executed. Plans and verifier verdicts therefore fail
closed too. Tool results apply the presentation policy to output, error, and nested metadata while
rejecting credential-bearing result identities. User task text, follow-ups, clarification answers,
and plan feedback are rejected before durable acceptance when they contain the configured key or a
token-shaped secret.

Leaf inspection is not the last check. Credentials are single-line values, and every durable JSON,
REST JSON, and WebSocket event uses a serializer that inserts a newline at each semantic JSON token
boundary. The finished serialization is scanned for raw and repeatedly JSON-escaped forms, so safe
individual fields cannot combine with quotes, commas, keys, or neighboring values to synthesize a
credential. SQLite keeps the current credential guard only in process memory; it checks run rows,
events, decisions, Proof Packs, and snapshot bytes before writes. Public projections recursively
redact registered keys and `sk-...` values, then scan the exact response bytes. There is no
unredacted or non-stream fallback after public partial output. Adversarial tests cover exact,
adjacent, token-shaped, marker-collision, JSON-escaped, nested-metadata, and structural-synthesis
cases across SQLite, REST, and WebSocket boundaries.

Native file mutations also inspect the proposed content and the existing UTF-8 file before taking a
rollback snapshot. A configured credential or token-shaped secret stops the operation before either
snapshot or write. This is an exact current-key/token boundary, not a general source-code secret
classifier.

An `assistant.output.started` row remains a durable provisional owner until an abort or
`turn.completed.final_stream_id` closes it. Startup aborts every open owner in the same transaction
that interrupts unfinished runs; post-provider validation failure, cancellation, resume, and
rollback use an idempotent ledger closure. Stream creation plus iteration has one wall-clock timeout;
compressed responses are rejected, HTTP bodies are byte-bounded before JSON parsing, transport
cancellation or boundary failure closes the async response, and aggregate limits prevent many
individually valid tool calls from bypassing resource bounds.

Provider exceptions are converted to bounded categories such as timeout, connection, rate limit,
request rejection, and contract failure. Raw SDK exception text is not copied into run errors,
because it may contain request bodies, credentials, or hidden provider fields.

On upgrade, every still-pending or durably accepted action approval created before this ingress
boundary is abandoned, its pending subject and private protocol messages are cleared, and it cannot
execute after resume. Existing immutable events and Proof Packs are not silently rewritten because
that would invalidate their evidence hashes. TraceForge can redact the currently configured key and
standard `sk-...` tokens at public read time, but it cannot infer an arbitrary nonstandard credential
that was rotated before upgrade. If an older TraceForge version may have committed a credential,
revoke it, replace it, and remove the old local database/history; deleting only a workspace file is
insufficient.

## Provider-private reasoning

Reasoning effort is an explicit per-turn request setting, not permission to reveal chain of thought.
The public API and settings panel advertise only levels known for an exact official endpoint/model
pair. `auto` omits the protocol field; unfamiliar OpenAI-compatible routes are default-only. This
prevents a deceptive hostname, port, query, userinfo, or family-looking model ID from gaining
provider-specific behavior.

DeepSeek thinking with native tools requires the assistant's `reasoning_content` to be replayed on
the next model request. TraceForge keeps that value only in its internal message record while the
turn is active or recoverable. It never enters a public event, REST run view, UI text, completion
summary, verifier evidence, or Proof Pack. Cross-provider message preparation removes it, so a
later OpenAI-compatible route cannot receive stale DeepSeek-private state. If the value matches a
configured credential or token-shaped `sk-...` pattern, the turn fails with a generic error before
the value is persisted; TraceForge never mutates the field and then replays corrupted protocol
state. Terminal answer, success, failure, cancellation, and rollback paths scrub and save the field
while the row is still non-terminal, then require a successful truncated SQLite WAL checkpoint
before persisting the terminal state. The checkpoint temporarily disables SQLite's multi-second
lock wait and uses a bounded short retry budget. A busy WAL records cleanup-pending state and moves
the task to recoverable `interrupted`; resume cannot call the model until cleanup succeeds, while
Stop can retry the cleanup without a process restart. Startup resolves pending cleanup and scrubs
legacy terminal rows left by older ordering. Adversarial tests cover sentinel reasoning,
token-shaped private state, terminal ordering, worker/state convergence, legacy cleanup, and an
external reader that keeps the WAL busy.

The safe `model.requested` event records the requested level, whether the wire field was omitted,
and whether DeepSeek thinking was enabled or disabled. It records no reasoning text. A selected
level is frozen across planner, builder, repairs, and verifier; if an interrupted run is pointed at
an incompatible route, resume is rejected before a model call instead of silently downgrading it.
The event remains available in the hashed audit ledger, but the default conversation and work-record
projection do not expose its provider/wire fields; the Timeline uses a generic model-call label.

## Persistence and rollback

SQLite and snapshots live in the platform user-data directory in real mode and in a temporary
directory in demo mode. They can contain task text, file snapshots, diffs, and command output. File
permissions are hardened to owner-only (`0700` directory and `0600` database/WAL/shared-memory
files) on POSIX. SQLite `secure_delete` is enabled and terminal reasoning scrubs force a truncated
WAL checkpoint whose busy result is checked, but TraceForge does not encrypt the database and does not claim forensic erasure
from SSDs, backups, or filesystem snapshots.

Rollback is hash-conditional. A file changed by the user after the agent wrote it is reported as a
conflict and preserved. This prevents recovery from becoming a second destructive action. Before
touching files, rollback permanently abandons the run's pending or accepted human decision and
clears its public subject; a stale response cannot race rollback or revive the gate even if a later
file operation fails. Resume and rollback are serialized by the same per-run lifecycle lock.

Filesystem changes cannot share a transaction with SQLite, so rollback makes each path
result-idempotent: an original file already restored, or a generated file already absent, is
recognized as the same successful outcome on retry. After the file batch, the `rolled_back` row and
complete result event commit together. Once that transaction exists, an exact API retry returns the
persisted result without running the file algorithm again, so a lost response cannot reinterpret
restored files as new conflicts.

A rolled-back run is never reopened for writing. Continuation creates one linked successor UUID
whose snapshot keys are independent and whose baseline is the current disk. Exact continuation
retries return that successor; a conflicting second branch is rejected. Thus a later rollback
cannot use the parent's older snapshots to overwrite work the user did after the first rollback.

The successful RunRecord, closed turn, terminal events, and immutable per-turn Proof Pack are one
SQLite transaction. A construction or identity conflict rolls back the whole success transition,
so the public state cannot claim `succeeded` before its evidence exists. The pack reads the exact
successful turn's final diff from its bounded persisted completion event, so later turns and user
edits do not silently change historical evidence. Its full-artifact, semantic-evidence, event-chain,
and Diff SHA-256 fingerprints provide integrity and comparison, not authenticity: TraceForge does
not sign the artifact, and a local actor who can rewrite the database can also recompute the hashes.
Proof responses disable HTTP caching; historical gaps from older versions are not reconstructed
from a newer workspace state.

Answered, failed, and cancelled runs use the same guarded transaction boundary for the RunRecord,
closed turn, and three terminal events (without a Proof Pack). Rollback includes an interrupted turn
closure when needed. Fault injection after the state update or turn event proves SQLite rolls the
entire boundary back, preventing terminal-state/turn divergence.

## Local web service

The CLI binds to `127.0.0.1` by default. WebSocket and mutating HTTP origins may use `localhost`
or the IPv4/IPv6 loopback address corresponding to the listener.
Before application construction, `serve` opens an owner-only regular lock file with symlink
following disabled, rejects unsafe ownership or hard links, holds a POSIX advisory lock for the
server lifetime, and pre-binds the requested listener. The file contains only PID, local URL,
application version, a random per-process identity, and a SHA-256 fingerprint of the canonical
workspace/host/port configuration—no task or credential data. Health exposes the random identity,
but not the configuration fingerprint. Reuse requires exact version, fingerprint, and health
identity equality after a bounded readiness wait. A legacy process without this protocol is
detected on both the requested URL and the historical default port and refused rather than bypassed
on a parallel port. This orders crash recovery after exclusive ownership and prevents a losing or
port-conflicted launch from marking the live instance's work as interrupted.
State-changing HTTP requests reject a non-local `Origin` and `Sec-Fetch-Site: cross-site`, which is
important because opening a local file manager is a host-side effect even though it does not edit
files. That endpoint accepts only a run id, never a browser-supplied path; it re-resolves the
persisted workspace and rejects a path replaced by a symlink before invoking fixed, shell-free
Finder/`xdg-open` argv. A Linux launcher discovered through `PATH` is resolved first and rejected
when it points inside the task workspace, preventing a project-local executable from impersonating
the file manager. Linux headless sessions report the capability as unsupported instead of
starting a hanging process. Security headers deny framing, MIME sniffing, and outbound content by
default. Binding to another interface expands the trust boundary and should only be done behind
controls chosen by the user.

The frontend also enforces a local evidence boundary between runs. A task switch invalidates old
Diff and Proof requests and detaches the previous event stream; late callbacks are ignored unless
their run owner and generation still match the selected run. Proof JSON must additionally carry
the requested `run_id`, as must every accepted event. The visible run is derived from the current
selection, per-run component state is remounted, task errors retain owner checks, and actions reject
a stale UI owner before dispatch. These checks prevent ordinary response or render reordering from
displaying one workspace's evidence—or targeting its controls—under another task, while the backend
remains the authority for access to each run-scoped route.

Decision and rollback forms also own a synchronous in-flight token, disable every mutable control,
and keep local failure details beside the relevant card/dialog. A rollback dialog cannot close via
Escape while its request is pending; success projects the backend's restored/removed/conflict
lists and receives focus. A successful mutation response is authoritative even if the subsequent
refresh fails, so that synchronization error cannot unlock the control and resend the POST. A
rolled-back continuation selects the new successor response rather than refreshing the old parent
ID; a lineage conflict refreshes the parent and task list so the existing successor becomes visible.

## Residual risks

- A user-approved bypass has the user's OS permissions and can ignore the workspace boundary.
- The label `full_access` is workspace-scoped in TraceForge; it is not permission to write arbitrary
  host paths or disable the OS sandbox.
- A backend profile is a containment layer, not a proof that malicious native code has no kernel or
  platform escape; unfamiliar hostile repositories still belong in a disposable VM.
- On Linux, OS enforcement requires Bubblewrap and usable unprivileged user namespaces. TraceForge
  fails visibly to `policy_only` when its startup probe cannot construct the namespace.
- The macOS profile permits loopback so project tests can bind local servers; another trusted local
  process could still be reachable.
- Read-only classification is syntactic; aliases/wrappers are treated as unknown, not trusted.
- A benign repository may contain arbitrary secrets in ordinary source files. TraceForge blocks the
  currently configured provider key and recognizable `sk-...` values at its durable/public
  boundaries, but it is not a general secret vault or repository-wide secret scanner.
- A root `AGENTS.md` is project-authored model input and can be malicious or simply wrong. Role
  placement, explicit precedence, immutable recovery, and local tool policy limit its authority but
  cannot guarantee that every model will follow useful guidance. Nested and inherited rules are
  deliberately unsupported in v1 rather than guessed.
- SQLite data remains until the platform data directory is removed.
- A process crash can leave provider-private reasoning in the owner-only database for an active or
  interrupted DeepSeek turn because protocol-correct resume may need it. A terminal transition is
  not persisted until the scrub checkpoint succeeds; an external long-lived SQLite reader can
  therefore move the task to a visible cleanup-pending interruption or defer startup instead of
  letting TraceForge claim cleanup prematurely.
- The risk assessment is deliberately conservative but still syntactic; Plan mode approval and
  planned file scope do not establish semantic safety.
- Proof Pack hashes are not signatures or remote attestations.
- Provider behavior and availability are external dependencies; retries do not guarantee service.
- An `uncertain` approved action may in fact have done nothing if the process died after the durable
  start marker but before entering the tool. Automatic replay would be more dangerous; inspect the
  workspace and external system before deciding whether to issue a new action.

The safe default for an unfamiliar repository is a disposable OS account, container, or virtual
machine in addition to TraceForge's built-in controls.

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
ancestor and checks it remains below the fixed workspace root. Absolute paths, traversal, `.git`
components, non-regular file targets, existing targets for `create_file`, and writes through any
symlink component are rejected.

Reads omit `.env` and `.env.*` except `.env.example`; directory listing and search also hide these
files. This reduces accidental disclosure to the model but is not a general secret vault.

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
or restart resolves a stale approval as `abandoned`, clears its ID, and never replays the action.

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
| macOS | built-in Seatbelt (`sandbox-exec`) | writes limited to workspace and command temp; external network denied; loopback retained |
| Linux | working non-setuid Bubblewrap | read-only host root, writable workspace/command temp, read-only `.git`, separate user/PID/network namespaces |
| Unsupported or failed probe | none (`policy_only`) | application path/argv policy and approval only |

Both enforced profiles block configured credential files, common credential locations, and real
`.env*` files other than `.env.example`. Seatbelt additionally denies ordinary file-content reads
under the user's home except the selected workspace and runtime paths. Bubblewrap keeps the host
root readable for tool compatibility but masks the enumerated sensitive paths. These are accurate,
different claims; the UI does not collapse them into a generic “secure” label.

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
its content must be exactly one non-empty line. Public status/configuration responses expose the
source and readiness flag, never the value. The connection check verifies native tool calling
rather than only endpoint reachability.

Before any model-proposed child command or local file-manager launcher starts, TraceForge removes environment variables whose
names indicate keys, passwords, passphrases, secrets, tokens, or credentials, as well as common
agent socket variables. Normal variables such as `PATH` remain available. Model and tool text are
also redacted for the configured credential and token-shaped `sk-...` values before public events
are emitted. The CI scanner examines Git-tracked UTF-8 files for private-key headers and common
credential literals.

Provider exceptions are converted to bounded categories such as timeout, connection, rate limit,
request rejection, and contract failure. Raw SDK exception text is not copied into run errors,
because it may contain request bodies, credentials, or hidden provider fields.

If a credential is ever committed, deleting the file is insufficient: revoke the credential,
replace it, and remove it from history before publishing.

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

## Persistence and rollback

SQLite and snapshots live in the platform user-data directory in real mode and in a temporary
directory in demo mode. They can contain task text, file snapshots, diffs, and command output. File
permissions are hardened to owner-only (`0700` directory and `0600` database/WAL/shared-memory
files) on POSIX. SQLite `secure_delete` is enabled and terminal reasoning scrubs force a truncated
WAL checkpoint whose busy result is checked, but TraceForge does not encrypt the database and does not claim forensic erasure
from SSDs, backups, or filesystem snapshots.

Rollback is hash-conditional. A file changed by the user after the agent wrote it is reported as a
conflict and preserved. This prevents recovery from becoming a second destructive action.

The Proof Pack reads the successful final diff from the persisted completion event, so later user
edits do not silently change historical evidence. Its SHA-256 fingerprints provide integrity and
comparison, not authenticity: TraceForge does not sign the artifact, and a local actor who can
rewrite the database can also recompute the hashes.

## Local web service

The CLI binds to `127.0.0.1` by default. WebSocket origins must be `localhost` or `127.0.0.1`.
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
- A benign repository may contain secrets in ordinary source files that the user asked the model
  to inspect.
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

The safe default for an unfamiliar repository is a disposable OS account, container, or virtual
machine in addition to TraceForge's built-in controls.

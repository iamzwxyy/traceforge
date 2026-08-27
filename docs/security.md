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

## File boundary

Every path must be a non-empty relative path. Before a read or write, TraceForge resolves the real
ancestor and checks it remains below the fixed workspace root. Absolute paths, traversal, `.git`
components, non-regular file targets, existing targets for `create_file`, and writes through any
symlink component are rejected.

Reads omit `.env` and `.env.*` except `.env.example`; directory listing and search also hide these
files. This reduces accidental disclosure to the model but is not a general secret vault.

## Plan and mutation policy

The model proposes a structured plan, but application code assigns the plan gate. The fast path is
limited to an explicit single-file scope, at most four steps and checks, no clarification, no
sensitive-area language in the task, plan, or risk notes, and routine local verification commands.
Generic implementation caveats do not turn a normal inspect/fix/verify plan into high risk. The plan
and policy reasons remain visible even when no click is required. Unknown scope, multiple files, credentials,
permissions, migrations, dependencies, deployment, deletion, or other high-impact language forces
human plan approval.

After planning, each `create_file` and every file in an `apply_patch` is compared with the declared
scope. An unexpected path pauses for a one-time action decision before any bytes are written. This
scope check remains an application policy even when an OS sandbox is active; it answers “was this
planned?” while the sandbox answers “what can this process technically reach?”

## Command policy

Commands are argv arrays passed to `asyncio.create_subprocess_exec`; shell parsing and expansion do
not occur.

| Class | Decision | Examples |
| --- | --- | --- |
| Approved acceptance check | allow + sandbox | exact argv recorded in the approved plan |
| Focused variant of an approved Pytest check | allow + sandbox, diagnostic only | same launcher family with a narrower selector or safe display/filter flags |
| Known read-only | allow + sandbox | `git status`, `git diff`, `rg`, `ls`, `cat` |
| Unknown / code execution | ask once; approval is a one-shot sandbox bypass | Python scripts, installers, network clients |
| Destructive / privileged | deny | `sudo`, `git reset --hard`, recursive forced `rm` |

An unknown command approval card shows the exact arguments, reason, and risk. Rejection returns a
normal failed tool result so the builder can choose a safer path. Approval bypasses the OS sandbox
for that invocation only; the resulting tool event, timeline badge, and Proof Pack record
`bypassed`. Commands default to 120 seconds, are capped at 600 seconds, and run in a separate
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

The real provider reads a credential from either `OPENAI_API_KEY` or a user-selected local file.
The file reference must resolve to a regular file smaller than 16 KiB; on POSIX it must be
owner-only, and its content must be exactly one non-empty line. SQLite stores only the canonical
file path. Public status/configuration responses expose the source and readiness flag, never the
value. The connection check verifies native tool calling rather than only endpoint reachability.

Before any model-proposed child command starts, TraceForge removes environment variables whose
names indicate keys, passwords, passphrases, secrets, tokens, or credentials, as well as common
agent socket variables. Normal variables such as `PATH` remain available. Model and tool text are
also redacted for the configured credential and token-shaped `sk-...` values before public events
are emitted. The CI scanner examines Git-tracked UTF-8 files for private-key headers and common
credential literals.

If a credential is ever committed, deleting the file is insufficient: revoke the credential,
replace it, and remove it from history before publishing.

## Persistence and rollback

SQLite and snapshots live in the platform user-data directory in real mode and in a temporary
directory in demo mode. They can contain task text, file snapshots, diffs, and command output. File
permissions follow the local user account; TraceForge does not encrypt this database.

Rollback is hash-conditional. A file changed by the user after the agent wrote it is reported as a
conflict and preserved. This prevents recovery from becoming a second destructive action.

The Proof Pack reads the successful final diff from the persisted completion event, so later user
edits do not silently change historical evidence. Its SHA-256 fingerprints provide integrity and
comparison, not authenticity: TraceForge does not sign the artifact, and a local actor who can
rewrite the database can also recompute the hashes.

## Local web service

The CLI binds to `127.0.0.1` by default. WebSocket origins must be `localhost` or `127.0.0.1`.
Security headers deny framing, MIME sniffing, and outbound content by default. Binding to another
interface expands the trust boundary and should only be done behind controls chosen by the user.

## Residual risks

- A user-approved bypass has the user's OS permissions and can ignore the workspace boundary.
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
- The risk gate is deliberately conservative but still syntactic; human approval and planned file
  scope do not establish semantic safety.
- Proof Pack hashes are not signatures or remote attestations.
- Provider behavior and availability are external dependencies; retries do not guarantee service.

The safe default for an unfamiliar repository is a disposable OS account, container, or virtual
machine in addition to TraceForge's built-in controls.

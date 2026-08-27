# Security model

TraceForge reduces accidental damage from model-proposed actions. It is not an OS sandbox and does
not claim to safely execute a malicious repository. The user account running TraceForge remains the
ultimate operating-system authority.

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

## Command policy

Commands are argv arrays passed to `asyncio.create_subprocess_exec`; shell parsing and expansion do
not occur.

| Class | Decision | Examples |
| --- | --- | --- |
| Approved acceptance check | allow | exact argv recorded in the approved plan |
| Known read-only | allow | `git status`, `git diff`, `rg`, `ls`, `cat` |
| Unknown / code execution | ask once | Python scripts, installers, network clients |
| Destructive / privileged | deny | `sudo`, `git reset --hard`, recursive forced `rm` |

An unknown command approval card shows the exact arguments, reason, and risk. Rejection returns a
normal failed tool result so the builder can choose a safer path. Commands default to 120 seconds,
are capped at 600 seconds, and run in a separate process group so Stop/timeout can terminate
descendants.

## Credentials and redaction

The real provider reads credentials from environment variables. `/api/status` returns only whether
a key is configured. Model and tool text are redacted for the configured key and token-shaped
`sk-...` values before public events are emitted. The CI scanner examines Git-tracked UTF-8 files
for private-key headers and common credential literals.

If a credential is ever committed, deleting the file is insufficient: revoke the credential,
replace it, and remove it from history before publishing.

## Persistence and rollback

SQLite and snapshots live in the platform user-data directory in real mode and in a temporary
directory in demo mode. They can contain task text, file snapshots, diffs, and command output. File
permissions follow the local user account; TraceForge does not encrypt this database.

Rollback is hash-conditional. A file changed by the user after the agent wrote it is reported as a
conflict and preserved. This prevents recovery from becoming a second destructive action.

## Local web service

The CLI binds to `127.0.0.1` by default. WebSocket origins must be `localhost` or `127.0.0.1`.
Security headers deny framing, MIME sniffing, and outbound content by default. Binding to another
interface expands the trust boundary and should only be done behind controls chosen by the user.

## Residual risks

- An approved command has the user's OS permissions and can ignore the workspace boundary.
- Read-only classification is syntactic; aliases/wrappers are treated as unknown, not trusted.
- A benign repository may contain secrets in ordinary source files that the user asked the model
  to inspect.
- SQLite data remains until the platform data directory is removed.
- Provider behavior and availability are external dependencies; retries do not guarantee service.

The safe default for an unfamiliar repository is a disposable OS account, container, or virtual
machine in addition to TraceForge's application-level controls.

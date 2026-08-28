# Command sandbox design and evidence

## Defensible claim

TraceForge does not use “approved” as a synonym for “sandboxed.” Each command records one of three
execution facts:

| Result | Meaning |
| --- | --- |
| `enforced` | a probed Seatbelt or Bubblewrap profile wrapped this invocation |
| `bypassed` | the user approved a base-policy unknown command for one Automatic-mode execution outside that profile |
| `policy_only` | no supported backend passed its probe; application policy still applied |

The application header reports current backend readiness. Each command row reports its own fact,
which matters because a run can be mixed. The Proof Pack aggregates the persisted command events
rather than trusting the current machine state. Commands rejected or denied before process launch
are counted separately and do not appear as misleading `policy_only` executions.

Action-permission profiles do not redefine these facts. Manual confirmations always pass
`bypass=False`; workspace Full access also passes `bypass=False` and auto-runs unknown commands
only when the backend is actually `enforced`. If the host is `policy_only`, that unknown command
falls back to a one-time human decision whose card explicitly warns about the bypass. Hard command
denials are repeated inside the executor so a caller cannot skip the base assessment and smuggle a
privileged, destructive, or outside-workspace argv into the bypass path.

## Profile construction

### macOS Seatbelt

TraceForge calls the exact built-in `/usr/bin/sandbox-exec` path after a trivial startup probe. The
profile uses parameter bindings for canonical workspace, command-temp, runtime, and sensitive
paths. It:

- permits writes only below the selected workspace and per-command temporary directory;
- protects the workspace root from unlink and makes `.git` read-only;
- denies external IPv4/IPv6 outbound connections while retaining loopback for local test servers;
- blocks configured credentials, common key stores, and `.env*` contents;
- denies other file-content reads under the user's home except workspace and required runtime
  roots, while retaining directory metadata needed to traverse a virtual-environment interpreter.

### Linux Bubblewrap

TraceForge accepts only a working, non-setuid `bwrap` outside the workspace. Its probe must create
user and network namespaces with a read-only root. Each invocation then uses a new session plus
user, PID, IPC, UTS, and network namespaces; rebinds only the workspace and command temp writable;
rebinds `.git` read-only; and masks known credential paths. If user namespaces are disabled or any
probe step fails, execution remains available but the state is visibly `policy_only`.

The Linux host root stays readable for broad build-tool compatibility. This is intentionally a
weaker read-isolation claim than the macOS home-content rule, while the write, process, and network
boundaries remain OS-enforced.

## Environment containment

Before either backend starts, TraceForge builds a child environment that:

- removes names matching key/password/passphrase/secret/token/credential and common agent sockets;
- points `HOME`, `TMPDIR`, `TMP`, `TEMP`, and XDG/package-manager caches at a fresh command temp;
- keeps a deterministic PATH with the project or TraceForge Python runtime first;
- deletes the entire command temp after completion, timeout, failed spawn, or cancellation.

This defense exists independently of file-system masking. A secret should not reach a child merely
because the current OS backend is degraded.

## Automated adversarial evidence

`tests/test_sandbox.py` runs real commands through the detected backend. When OS enforcement is
available it proves:

1. a command can create a normal file inside the selected workspace;
2. the same interpreter cannot write to a sibling path outside that workspace;
3. project `.env` / configured credential contents cannot be read;
4. an explicit bypass can perform the otherwise-blocked write, and the result says `bypassed`.

The permission matrix additionally proves that Manual planned checks stay sandboxed, workspace
Full access never requests a bypass, hard denials survive all three profiles, and an unknown Full-
access command cannot auto-run on a policy-only host.

The tests skip only the backend-dependent attacks when the machine honestly reports
`policy_only`; profile-construction, API status, event aggregation, and bypass evidence remain
portable tests. The browser E2E also checks the global state and Proof Pack sandbox card.

## Residual boundary

This profile protects the normal local-agent workflow; it is not a kernel exploit boundary or a
remote attestation. macOS loopback remains reachable. Linux readable host files outside enumerated
sensitive paths may still disclose data to a malicious build. A user-approved bypass intentionally
runs with the TraceForge user's authority. Use a disposable VM for intentionally hostile code.

Design references: [OpenAI Codex sandboxing](https://developers.openai.com/codex/sandboxing) and
[Bubblewrap](https://github.com/containers/bubblewrap).

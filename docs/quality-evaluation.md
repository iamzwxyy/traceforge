# Fixed product-quality corpus

## Why this exists

TraceForge's main claims are behavioral: it should not guess past ambiguity, claim success with
stale evidence, replay an interrupted side effect, overwrite a later user edit, or call an approval
a sandbox. A large undifferentiated test count does not make those claims easy to audit.

The fixed corpus selects five representative product risks and maps each one to real core tests.
It adds no user-facing feature and uses no model credential. The command runs each scenario in a
fresh Pytest process, then produces a concise Markdown or JSON scorecard.

```bash
uv run python scripts/evaluate_quality.py
uv run python scripts/evaluate_quality.py --format json
uv run python scripts/evaluate_quality.py --require-os-sandbox
```

## Scenarios and invariants

| Scenario | Invariants that must hold | Authoritative evidence |
| --- | --- | --- |
| Complete evidence loop | real diff, fresh checks, independent pass, stable completion diff and Proof Pack digest | deterministic tenant-isolation demo plus Proof Pack projection test |
| Risk-adaptive human control | low-risk plan stays visible; scope drift and unknown execution pause before action | plan-gate and approval state-machine tests |
| Truthful repair and termination | edits invalidate old checks; repair-budget exhaustion ends failed | stale-check repair and repair-limit tests |
| Recovery and rollback | incomplete tool call receives a synthetic failure and is never replayed; one conflict does not block safe rollback of other files | restart protocol and partial rollback tests |
| Command isolation | enforced backend blocks workspace escape and credential reads; an explicit escape is one-shot and labeled | real Seatbelt/Bubblewrap adversarial tests plus bypass evidence test |

`passed` means every selected invariant executed and passed. `degraded` means Pytest skipped an
environment-dependent invariant—normally because the host honestly reports `policy_only` instead
of an OS sandbox. `failed` means an invariant contradicted the claim or timed out. The default
command returns success for an honest degraded host; `--require-os-sandbox` makes enforcement a
hard gate for a release machine.

## What this does not prove

- It is a deterministic regression corpus, not a statistical model-quality benchmark.
- It does not replace the full backend/frontend gates or the browser flow.
- The fake provider proves orchestration semantics, not that every compatible model plans well.
- OS-sandbox success is local to the tested backend and profile; it is not a remote attestation.

Real-model representative tasks remain a separate, low-frequency acceptance layer so everyday CI
does not depend on an external key, endpoint availability, or model drift.

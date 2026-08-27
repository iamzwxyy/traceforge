# Real-model representative evaluation

## Purpose

The deterministic quality corpus proves TraceForge's state-machine invariants without a network or
credential. It cannot prove that a real OpenAI-compatible model follows the tool schemas, chooses a
useful plan, or finishes ordinary repository work without approval fatigue. This separate,
low-frequency layer exercises those integration risks before a release or important demo.

The evaluator copies two pinned faulty projects into disposable directories, drives TraceForge with
the configured provider, and selects recommended clarification answers. It approves a visible plan
when the deterministic gate requires review, but rejects every unplanned action approval. After the
run, the harness independently runs the full test suite and a hidden semantic check. A scenario
passes only when all of these facts hold:

- the fixture's pinned precondition is present and its hidden check fails before the repair;
- the run succeeds through the expected plan-gate path with no unplanned action prompt;
- the expected implementation and regression files change, with no scope drift on the single-file
  scenario;
- all planned checks are fresh, the read-only verifier passes, and the Proof Pack is `proven`;
- an independent full Pytest run and the hidden semantic check pass after the repair.

## Pinned scenarios

| Scenario | Fault shape | Expected control path | Independent oracle |
| --- | --- | --- | --- |
| Single-file duration parser | Python accepts `bool` as an `int`; public tests start at 8 passed / 2 failed | visible `auto_approved` low-risk plan; only `duration_parser.py` may change | all 10 tests pass; booleans raise `TypeError`; normal integers are preserved |
| Multi-file tenant cache | public tests start green while equal profile IDs leak values across tenants | `approval_required`; implementation plus regression tests must change | full suite passes; two tenants remain isolated and same-tenant cache hits persist |

The single-file fixture lives under `evaluation/fixtures/duration-parser`. The multi-file fixture is
the same tenant-cache project used by the deterministic demo, so the real and replayable stories
exercise one product claim through complementary evidence.

## Running it safely

The credential file must contain one non-empty line and be owner-only. The value is resolved inside
provider construction; the evaluator prints only the source type and never the value.

```bash
chmod 600 /absolute/path/to/provider-key

uv run python scripts/evaluate_real_model.py \
  --credential-file /absolute/path/to/provider-key \
  --model deepseek-v4-flash-vision-exp \
  --base-url https://api.deepseek.com \
  --output /tmp/traceforge-real-model-report.json
```

Use `--scenario single-file-fast-path` or `--scenario multi-file-review-path` to isolate one case.
The command is intentionally absent from CI: endpoint availability, cost, rate limits, and model
drift should not make deterministic pull-request checks flaky.

## 2026-08-27 DeepSeek findings

Model: `deepseek-v4-flash-vision-exp`. Host: macOS with an enforced Seatbelt command sandbox.

The first single-file trial repaired the code correctly and passed both the exact 10-test suite and
the hidden check, but the run still failed after 17 steps and seven rejected action prompts. The
model repeatedly requested focused Pytest selectors that differed from the exact planned argv.
Proof projection also counted those rejected, never-executed commands as policy-only execution.

The product was changed in response, not the task:

- non-writing, non-interactive focused Pytest variants may run under the existing sandbox without a
  second prompt, while different launchers, interactive/output-writing flags, arbitrary Python,
  network clients, and mutating Ruff commands still pause or fail the routine gate;
- a focused variant is diagnostic only; completion still requires the exact planned argv after the
  final mutation;
- rejected commands are counted as `blocked before run`, not as executed policy-only commands;
- ordinary four-step inspect/fix/verify plans remain eligible for the single-file fast path, while
  sensitive risk notes and broader scope still require review.

The pinned single-file evaluator then passed in 3 tool steps and 35 persisted events: one changed
file, 10 independent tests, fresh checks, verifier `pass`, Proof Pack `proven`, one Seatbelt-enforced
command, and zero action prompts.

The first pinned multi-file evaluator found a different availability defect before any edit: the
model returned `risks` as structured risk/mitigation objects even though the native schema requires
strings. A Pydantic validation error terminated planning. Invalid clarification or plan payloads
now become auditable failed tool results with a corrective prompt, allowing the model to repair the
schema within the same run. The evaluator also sets `PYTHONPATH=src` for independent checks so its
hidden oracle tests behavior rather than failing on packaging layout.

The identical multi-file task then passed in 13 tool steps and 89 persisted events. The gate was
`approval_required/high`; implementation and two test files changed; the independent suite grew
from 3 to 5 passing tests; the hidden isolation oracle passed; checks were fresh; the verifier and
Proof Pack passed; one command was Seatbelt-enforced; and there were zero unplanned action prompts.

These are representative acceptance samples, not a statistical model leaderboard. They establish
that both the low-friction and reviewed paths work with a real provider and that two real-model
integration failures produced regression-tested product changes.

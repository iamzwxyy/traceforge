#!/usr/bin/env python3
"""Run TraceForge's fixed, deterministic product-quality corpus."""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from traceforge.sandbox import sandbox_status

ROOT = Path(__file__).resolve().parents[1]
ScenarioStatus = Literal["passed", "degraded", "failed"]


@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    title: str
    claim: str
    tests: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    id: str
    title: str
    claim: str
    status: ScenarioStatus
    duration_seconds: float
    tests: tuple[str, ...]
    pytest_summary: str
    output_tail: str = ""


SCENARIOS = (
    Scenario(
        id="complete-loop",
        title="Complete evidence loop",
        claim=(
            "A real edit reaches fresh checks, independent verification, and a stable Proof Pack."
        ),
        tests=(
            "tests/test_demo.py::test_scripted_demo_completes_with_real_test_evidence",
            "tests/test_proof.py::test_proof_pack_aggregates_stable_completion_evidence",
        ),
    ),
    Scenario(
        id="human-control",
        title="Mode-aware intent and human control",
        claim=(
            "Conversation answers without a false workflow; executable Agent work proceeds "
            "without a plan pause, Plan mode waits for review, and scope drift or unknown "
            "execution still requires a decision."
        ),
        tests=(
            (
                "tests/test_agent.py::"
                "test_conversational_request_is_answered_without_false_workflow"
            ),
            (
                "tests/test_agent.py::"
                "test_agent_mode_continues_without_plan_approval_but_stays_visible"
            ),
            "tests/test_agent.py::test_plan_mode_always_pauses_for_review_even_when_low_risk",
            "tests/test_agent.py::test_fast_path_scope_drift_requires_action_approval",
            "tests/test_agent.py::test_unknown_command_waits_for_explicit_approval",
        ),
    ),
    Scenario(
        id="truthful-repair",
        title="Truthful repair and termination",
        claim=(
            "A repair invalidates stale checks, and exhausted repair budgets fail instead of "
            "claiming completion."
        ),
        tests=(
            "tests/test_agent.py::test_repair_cannot_reuse_a_passing_check_from_before_the_edit",
            "tests/test_agent.py::test_verifier_failure_at_repair_limit_fails_run",
        ),
    ),
    Scenario(
        id="recovery-rollback",
        title="Recovery without duplicated side effects",
        claim=(
            "Transient model outages pause with retry evidence, request-bound decisions resume "
            "without crossing prompts, started actions are never replayed, and rollback keeps a "
            "fresh successor boundary while preserving later user edits."
        ),
        tests=(
            (
                "tests/test_agent.py::"
                "test_transient_model_outage_pauses_and_resumes_without_losing_run"
            ),
            "tests/test_agent.py::test_resume_repairs_only_the_missing_result_in_a_partial_tool_batch",
            (
                "tests/test_agent.py::"
                "test_accepted_clarification_crash_window_pairs_answer_exactly_once"
            ),
            (
                "tests/test_agent.py::"
                "test_started_approved_action_is_uncertain_and_never_replayed_after_crash"
            ),
            (
                "tests/test_agent.py::"
                "test_rollback_successor_uses_fresh_snapshot_boundary_and_lineage"
            ),
            (
                "tests/test_workspace.py::"
                "test_rollback_restores_safe_files_while_preserving_one_conflict"
            ),
        ),
    ),
    Scenario(
        id="command-isolation",
        title="Command isolation with auditable escape",
        claim=(
            "An enforced backend blocks workspace escape and secret reads; an approved bypass is "
            "one-shot and visible."
        ),
        tests=(
            (
                "tests/test_sandbox.py::"
                "test_enforced_sandbox_allows_workspace_write_and_blocks_escape"
            ),
            "tests/test_sandbox.py::test_enforced_sandbox_blocks_secret_file_contents",
            "tests/test_sandbox.py::test_explicit_sandbox_bypass_is_visible_and_one_shot",
        ),
    ),
    Scenario(
        id="streaming-integrity",
        title="Durable, redacted, and boundary-safe output",
        claim=(
            "Visible output is incrementally persisted without credential leakage; semantic "
            "provider data cannot carry a credential into execution; JSON structure, native "
            "snapshots, SQLite, REST, and WebSocket fail closed; retries, cancellation, and "
            "restart remain isolated; rotated-credential conflicts across guidance/history, "
            "protocol text, timestamps, and old stream identifiers can be stopped without model "
            "or tool side effects; and exactly one stream plus its terminal turn is canonical."
        ),
        tests=(
            (
                "tests/test_streaming.py::"
                "test_streaming_redactor_never_releases_a_secret_at_any_chunk_boundary"
            ),
            (
                "tests/test_streaming.py::"
                "test_streaming_redactor_is_stable_for_adjacent_secrets_and_every_prefix"
            ),
            "tests/test_streaming.py::test_redaction_marker_collision_never_releases_configured_key",
            (
                "tests/test_streaming.py::"
                "test_assistant_storage_redacts_escaped_credentials_before_json_serialization"
            ),
            (
                "tests/test_streaming.py::"
                "test_boundary_safe_json_cannot_synthesize_a_single_line_key_from_structure"
            ),
            (
                "tests/test_agent.py::"
                "test_provider_tool_credentials_fail_before_execution_or_persistence"
            ),
            (
                "tests/test_agent.py::"
                "test_structural_json_cannot_synthesize_a_credential_in_agent_history"
            ),
            (
                "tests/test_agent.py::"
                "test_tool_result_metadata_is_recursively_redacted_before_persistence"
            ),
            (
                "tests/test_agent.py::"
                "test_unsafe_accepted_legacy_action_requires_destructive_cancel_without_execution"
            ),
            (
                "tests/test_agent.py::"
                "test_rotated_credential_context_can_be_cancelled_without_model_or_tool_side_effects"
            ),
            (
                "tests/test_agent.py::"
                "test_rotated_credential_cancellation_abandons_every_active_decision_kind"
            ),
            (
                "tests/test_agent.py::"
                "test_resume_rejects_compact_json_credential_synthesized_from_safe_leaves"
            ),
            (
                "tests/test_workspace_instructions.py::"
                "test_resume_preflight_rejects_compact_boundary_between_snapshot_and_history"
            ),
            (
                "tests/test_runtime.py::"
                "test_provider_credential_rejects_unrecoverable_protocol_collisions"
            ),
            "tests/test_agent.py::test_credential_conflict_cancel_chooses_a_safe_timestamp",
            (
                "tests/test_agent.py::"
                "test_credential_conflict_cancel_does_not_copy_an_unsafe_stream_id"
            ),
            (
                "tests/test_storage.py::"
                "test_registered_credential_guard_rejects_run_event_and_snapshot_writes"
            ),
            (
                "tests/test_api.py::"
                "test_rest_and_websocket_json_do_not_synthesize_a_configured_key"
            ),
            (
                "tests/test_tools.py::"
                "test_native_mutations_never_snapshot_or_write_credentials"
            ),
            "tests/test_streaming.py::test_openai_stream_rejects_unsafe_protocol_shapes",
            (
                "tests/test_streaming.py::"
                "test_raw_transport_rejects_oversized_and_compressed_bodies_before_json"
            ),
            (
                "tests/test_streaming.py::"
                "test_agent_streams_one_canonical_direct_answer_without_secret_persistence"
            ),
            (
                "tests/test_streaming.py::"
                "test_retry_uses_distinct_streams_and_commits_only_the_successor"
            ),
            (
                "tests/test_streaming.py::"
                "test_verified_finish_summary_stream_is_committed_by_the_success_event"
            ),
            (
                "tests/test_streaming.py::"
                "test_cancelling_a_live_stream_persists_an_aborted_partial"
            ),
            (
                "tests/test_streaming.py::"
                "test_process_restart_atomically_aborts_uncommitted_stream_generation"
            ),
            (
                "tests/test_streaming.py::"
                "test_post_stream_private_reasoning_rejection_aborts_before_provider_completion"
            ),
            (
                "tests/test_streaming.py::"
                "test_answer_commit_fault_cannot_leave_a_terminal_run_with_an_open_turn"
            ),
            (
                "tests/test_storage.py::"
                "test_interruption_commit_fault_rolls_back_stream_state_and_events_together"
            ),
        ),
    ),
)
SCENARIO_BY_ID = {scenario.id: scenario for scenario in SCENARIOS}


def evaluate(selected: tuple[Scenario, ...], *, timeout_seconds: int) -> dict[str, object]:
    results = [_run_scenario(scenario, timeout_seconds=timeout_seconds) for scenario in selected]
    counts = {
        status: sum(result.status == status for result in results)
        for status in ("passed", "degraded", "failed")
    }
    if counts["failed"]:
        overall: ScenarioStatus = "failed"
    elif counts["degraded"]:
        overall = "degraded"
    else:
        overall = "passed"
    current_sandbox = sandbox_status(ROOT)
    return {
        "schema_version": "traceforge.quality-corpus.v1",
        "overall": overall,
        "counts": counts,
        "environment": {
            "platform": platform.system().lower(),
            "python": platform.python_version(),
            "sandbox": current_sandbox.as_dict(),
        },
        "scenarios": [asdict(result) for result in results],
    }


def _run_scenario(scenario: Scenario, *, timeout_seconds: int) -> ScenarioResult:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *scenario.tests],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        output = _timeout_output(exc)
        return ScenarioResult(
            id=scenario.id,
            title=scenario.title,
            claim=scenario.claim,
            status="failed",
            duration_seconds=round(time.perf_counter() - started, 3),
            tests=scenario.tests,
            pytest_summary=f"timed out after {timeout_seconds}s",
            output_tail=output[-4_000:],
        )

    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    skipped = _pytest_count(output, "skipped")
    status: ScenarioStatus
    if completed.returncode != 0:
        status = "failed"
    elif skipped:
        status = "degraded"
    else:
        status = "passed"
    return ScenarioResult(
        id=scenario.id,
        title=scenario.title,
        claim=scenario.claim,
        status=status,
        duration_seconds=round(time.perf_counter() - started, 3),
        tests=scenario.tests,
        pytest_summary=_pytest_summary(output),
        output_tail=output[-4_000:] if status == "failed" else "",
    )


def _pytest_count(output: str, label: str) -> int:
    match = re.search(rf"(\d+) {re.escape(label)}", output)
    return int(match.group(1)) if match else 0


def _pytest_summary(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        if re.search(r"\b(passed|failed|skipped|error|errors)\b", line):
            return line
    return lines[-1] if lines else "pytest produced no output"


def _timeout_output(exc: subprocess.TimeoutExpired) -> str:
    stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout
    stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
    return "\n".join(part for part in (stdout, stderr) if part)


def render_markdown(report: dict[str, object]) -> str:
    counts = report["counts"]
    environment = report["environment"]
    scenarios = report["scenarios"]
    assert isinstance(counts, dict)
    assert isinstance(environment, dict)
    assert isinstance(scenarios, list)
    sandbox = environment["sandbox"]
    assert isinstance(sandbox, dict)
    rows = [
        "# TraceForge quality corpus",
        "",
        f"Overall: **{str(report['overall']).upper()}**",
        (
            f"Sandbox: **{sandbox['backend']}** · "
            f"{'OS enforced' if sandbox['enforced'] else 'policy only'}"
        ),
        "",
        "| Scenario | Result | Defensible claim | Pytest evidence |",
        "| --- | --- | --- | --- |",
    ]
    for raw in scenarios:
        assert isinstance(raw, dict)
        rows.append(
            "| "
            + " | ".join(
                [
                    str(raw["title"]),
                    f"**{str(raw['status']).upper()}**",
                    str(raw["claim"]),
                    str(raw["pytest_summary"]),
                ]
            )
            + " |"
        )
    rows.extend(
        [
            "",
            (
                "Result: "
                f"{counts['passed']} passed, {counts['degraded']} degraded, "
                f"{counts['failed']} failed."
            ),
        ]
    )
    for raw in scenarios:
        assert isinstance(raw, dict)
        if raw.get("output_tail"):
            rows.extend(
                [
                    "",
                    f"## Failure · {raw['title']}",
                    "",
                    "```text",
                    str(raw["output_tail"]),
                    "```",
                ]
            )
    return "\n".join(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        action="append",
        choices=tuple(SCENARIO_BY_ID),
        help="run only this scenario; repeat to select several",
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--list", action="store_true", help="list scenario ids and exit")
    parser.add_argument(
        "--require-os-sandbox",
        action="store_true",
        help="treat a policy-only host as a failed quality gate",
    )
    parser.add_argument("--timeout-seconds", type=int, default=120)
    args = parser.parse_args()

    if args.list:
        for scenario in SCENARIOS:
            print(f"{scenario.id}\t{scenario.title}")
        return 0
    if args.timeout_seconds < 1:
        parser.error("--timeout-seconds must be at least 1")

    selected = (
        tuple(SCENARIO_BY_ID[scenario_id] for scenario_id in args.scenario)
        if args.scenario
        else SCENARIOS
    )
    report = evaluate(selected, timeout_seconds=args.timeout_seconds)
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_markdown(report))

    if report["overall"] == "failed":
        return 1
    environment = report["environment"]
    assert isinstance(environment, dict)
    current_sandbox = environment["sandbox"]
    assert isinstance(current_sandbox, dict)
    if args.require_os_sandbox and not current_sandbox["enforced"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

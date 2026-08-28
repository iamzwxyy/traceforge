#!/usr/bin/env python3
"""Run low-frequency, credentialed TraceForge acceptance scenarios."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

from traceforge.agent import PlanDecision
from traceforge.config import Settings
from traceforge.demo import DEMO_TASK
from traceforge.events import EventBroker
from traceforge.models import (
    ClarificationAnswer,
    InteractionMode,
    ProviderConfig,
    ReasoningEffort,
    RunState,
)
from traceforge.proof import build_proof_pack
from traceforge.runtime import AgentRuntime, validate_credential_file
from traceforge.storage import Storage

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "deepseek-v4-flash-vision-exp"
DEFAULT_BASE_URL = "https://api.deepseek.com"
_SENSITIVE_ENV_PATTERN = re.compile(
    r"KEY|PASSWORD|PASSWD|PASSPHRASE|SECRET|TOKEN|CREDENTIAL", re.IGNORECASE
)
@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    title: str
    fixture: Path
    task: str
    mode: InteractionMode
    expected_gate: Literal["agent_continues", "approval_required"]
    required_files: tuple[str, ...]
    allowed_files: tuple[str, ...] | None
    baseline_pytest_exit: int
    hidden_program: str


SCENARIOS = (
    Scenario(
        id="single-file-fast-path",
        title="Single-file repair in default Agent mode",
        fixture=ROOT / "evaluation/fixtures/duration-parser",
        task=(
            "Fix the boolean-input bug described in README.md. Preserve normalize_seconds's "
            "public signature and all existing integer behavior. Only modify duration_parser.py; "
            "do not edit tests. Run the full test suite and finish only when it passes."
        ),
        mode=InteractionMode.AGENT,
        expected_gate="agent_continues",
        required_files=("duration_parser.py",),
        allowed_files=("duration_parser.py",),
        baseline_pytest_exit=1,
        hidden_program=(
            "from duration_parser import normalize_seconds\n"
            "for value in (True, False):\n"
            "    try:\n"
            "        normalize_seconds(value)\n"
            "    except TypeError:\n"
            "        continue\n"
            "    raise AssertionError(f'{value!r} was accepted')\n"
            "assert normalize_seconds(0) == 0\n"
            "assert normalize_seconds(3600) == 3600\n"
        ),
    ),
    Scenario(
        id="multi-file-review-path",
        title="Multi-file repair behind plan review",
        fixture=ROOT / "demo/tenant-cache-api",
        task=DEMO_TASK,
        mode=InteractionMode.PLAN,
        expected_gate="approval_required",
        required_files=("src/tenant_cache_api/cache.py",),
        allowed_files=None,
        baseline_pytest_exit=0,
        hidden_program=(
            "from tenant_cache_api.cache import TenantTTLCache\n"
            "cache = TenantTTLCache(clock=lambda: 10)\n"
            "assert cache.get_or_load('acme', '42', lambda: 'Ada') == 'Ada'\n"
            "assert cache.get_or_load('globex', '42', lambda: 'Grace') == 'Grace'\n"
            "assert cache.get_or_load('acme', '42', lambda: 'wrong') == 'Ada'\n"
        ),
    ),
)
SCENARIO_BY_ID = {scenario.id: scenario for scenario in SCENARIOS}


def _copy_fixture(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "uv.lock", "__pycache__", ".pytest_cache"
        ),
    )


def _run_host_check(workspace: Path, argv: list[str], *, timeout: int = 60) -> dict[str, Any]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if _SENSITIVE_ENV_PATTERN.search(key) is None
    }
    source_root = workspace / "src"
    if source_root.is_dir():
        environment["PYTHONPATH"] = str(source_root)
    completed = subprocess.run(
        argv,
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return {
        "exit_code": completed.returncode,
        "summary": lines[-1] if lines else "No output",
    }


def _scenario_failures(
    scenario: Scenario,
    *,
    state: RunState,
    gate: str | None,
    changed_files: list[str],
    checks_fresh: bool,
    verdict: str | None,
    proof_status: str,
    action_prompts: list[dict[str, Any]],
    baseline_pytest: dict[str, Any],
    baseline_hidden: dict[str, Any],
    independent_pytest: dict[str, Any],
    hidden_check: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    if baseline_pytest["exit_code"] != scenario.baseline_pytest_exit:
        failures.append("fixture Pytest baseline did not match the pinned precondition")
    if baseline_hidden["exit_code"] == 0:
        failures.append("hidden semantic check did not fail before the repair")
    if state is not RunState.SUCCEEDED:
        failures.append(f"run ended in {state.value}, not succeeded")
    if gate != scenario.expected_gate:
        failures.append(f"plan gate was {gate}, expected {scenario.expected_gate}")
    missing = sorted(set(scenario.required_files) - set(changed_files))
    if missing:
        failures.append("required changed files missing: " + ", ".join(missing))
    if scenario.allowed_files is not None:
        unexpected = sorted(set(changed_files) - set(scenario.allowed_files))
        if unexpected:
            failures.append("unexpected changed files: " + ", ".join(unexpected))
    else:
        if not any(path.startswith("tests/") for path in changed_files):
            failures.append("multi-file scenario did not add or update a regression test")
    if not checks_fresh:
        failures.append("planned acceptance checks were stale")
    if verdict != "pass":
        failures.append(f"independent verifier verdict was {verdict}")
    if proof_status != "proven":
        failures.append(f"Proof Pack status was {proof_status}")
    if action_prompts:
        failures.append(f"encountered {len(action_prompts)} unplanned action approval(s)")
    if independent_pytest["exit_code"] != 0:
        failures.append("independent full Pytest run failed")
    if hidden_check["exit_code"] != 0:
        failures.append("independent hidden semantic check failed")
    return failures


async def _drive_run(
    scenario: Scenario,
    workspace: Path,
    *,
    credential_file: Path,
    model: str,
    base_url: str,
    reasoning_effort: ReasoningEffort,
    timeout_seconds: int,
) -> dict[str, Any]:
    baseline_pytest = _run_host_check(workspace, [sys.executable, "-m", "pytest", "-q"])
    baseline_hidden = _run_host_check(
        workspace, [sys.executable, "-c", scenario.hidden_program], timeout=10
    )
    data_dir = workspace.parent / "data"
    settings = Settings(
        workspace=workspace,
        data_dir=data_dir,
        api_key="",
        base_url=None,
        model=model,
    )
    storage = Storage(data_dir / "evaluation.db")
    storage.save_provider_config(
        ProviderConfig(
            model=model,
            base_url=base_url,
            credential_file=str(credential_file),
        )
    )
    runtime = AgentRuntime(settings, storage, EventBroker(storage))
    clarification_rounds: list[dict[str, Any]] = []
    plan_reviews: list[dict[str, Any]] = []
    action_prompts: list[dict[str, Any]] = []
    handled_clarifications: set[tuple[int, tuple[str, ...]]] = set()
    handled_approvals: set[str] = set()
    started = time.perf_counter()

    try:
        run = await runtime.start_run(
            scenario.task,
            workspace,
            mode=scenario.mode,
            reasoning_effort=reasoning_effort,
        )
        manager = runtime.manager_for_run(run.id)
        async with asyncio.timeout(timeout_seconds):
            while True:
                current = storage.get_run(run.id)
                if current.state.terminal or current.state is RunState.INTERRUPTED:
                    break
                if current.state is RunState.AWAITING_CLARIFICATION and current.clarification:
                    key = (
                        current.clarification.round,
                        tuple(question.id for question in current.clarification.questions),
                    )
                    if key not in handled_clarifications:
                        answers: list[ClarificationAnswer] = []
                        choices: list[dict[str, str]] = []
                        for question in current.clarification.questions:
                            option = next(
                                (item for item in question.options if item.recommended),
                                question.options[0],
                            )
                            choices.append({"question": question.prompt, "answer": option.label})
                            answers.append(
                                ClarificationAnswer(
                                    question_id=question.id,
                                    option_id=option.id,
                                )
                            )
                        clarification_rounds.append(
                            {"round": current.clarification.round, "choices": choices}
                        )
                        print(
                            f"[{scenario.id}] selected recommended clarification answers",
                            file=sys.stderr,
                            flush=True,
                        )
                        await manager.answer_clarification(run.id, answers)
                        handled_clarifications.add(key)
                elif current.state is RunState.AWAITING_PLAN_APPROVAL and current.plan:
                    approval_key = current.plan.summary
                    if approval_key not in handled_approvals:
                        plan_reviews.append(
                            {
                                "gate": (
                                    current.plan_gate.decision if current.plan_gate else None
                                ),
                                "risk": current.plan_gate.risk if current.plan_gate else None,
                                "impacted_files": current.plan.impacted_files,
                                "steps": len(current.plan.steps),
                                "checks": [
                                    check.command for check in current.plan.acceptance_checks
                                ],
                            }
                        )
                        print(
                            f"[{scenario.id}] approved the visible evaluation plan",
                            file=sys.stderr,
                            flush=True,
                        )
                        await manager.decide_plan(run.id, PlanDecision(decision="approve"))
                        handled_approvals.add(approval_key)
                elif current.state is RunState.AWAITING_ACTION_APPROVAL:
                    approval = current.pending_approval
                    if approval and approval.id not in handled_approvals:
                        action_prompts.append(
                            {
                                "tool": approval.tool_call.name,
                                "argv": approval.tool_call.arguments.get("argv"),
                                "reason": approval.reason,
                            }
                        )
                        print(
                            f"[{scenario.id}] rejected an unplanned action approval",
                            file=sys.stderr,
                            flush=True,
                        )
                        await manager.decide_action(run.id, approved=False)
                        handled_approvals.add(approval.id)
                await asyncio.sleep(0.05)

        completed = await manager.wait(run.id)
        proof = build_proof_pack(completed, storage)
        independent_pytest = _run_host_check(
            workspace, [sys.executable, "-m", "pytest", "-q"]
        )
        hidden_check = _run_host_check(
            workspace, [sys.executable, "-c", scenario.hidden_program], timeout=10
        )
        gate = completed.plan_gate.decision if completed.plan_gate else None
        verdict = completed.verification.verdict.value if completed.verification else None
        failures = _scenario_failures(
            scenario,
            state=completed.state,
            gate=gate,
            changed_files=proof.changed_files,
            checks_fresh=proof.checks_fresh,
            verdict=verdict,
            proof_status=proof.proof_status,
            action_prompts=action_prompts,
            baseline_pytest=baseline_pytest,
            baseline_hidden=baseline_hidden,
            independent_pytest=independent_pytest,
            hidden_check=hidden_check,
        )
        return {
            "id": scenario.id,
            "title": scenario.title,
            "status": "failed" if failures else "passed",
            "duration_seconds": round(time.perf_counter() - started, 3),
            "state": completed.state.value,
            "run_error": completed.error,
            "plan_gate": (
                completed.plan_gate.model_dump(mode="json")
                if completed.plan_gate
                else None
            ),
            "changed_files": proof.changed_files,
            "checks_fresh": proof.checks_fresh,
            "verdict": verdict,
            "proof_status": proof.proof_status,
            "sandbox": proof.command_sandbox.model_dump(mode="json"),
            "steps": completed.step_count,
            "repair_cycles": completed.repair_cycles,
            "event_count": proof.event_count,
            "reasoning_effort": completed.reasoning_effort.value,
            "clarification_rounds": clarification_rounds,
            "plan_reviews": plan_reviews,
            "action_prompts": action_prompts,
            "baseline_pytest": baseline_pytest,
            "baseline_hidden": baseline_hidden,
            "independent_pytest": independent_pytest,
            "hidden_check": hidden_check,
            "failures": failures,
        }
    finally:
        await runtime.shutdown()
        storage.close()


async def evaluate(
    scenarios: tuple[Scenario, ...],
    *,
    credential_file: Path,
    model: str,
    base_url: str,
    reasoning_effort: ReasoningEffort,
    timeout_seconds: int,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for scenario in scenarios:
        print(f"[{scenario.id}] starting", file=sys.stderr, flush=True)
        try:
            with TemporaryDirectory(prefix=f"traceforge-{scenario.id}-") as temporary:
                workspace = Path(temporary) / "workspace"
                _copy_fixture(scenario.fixture, workspace)
                result = await _drive_run(
                    scenario,
                    workspace,
                    credential_file=credential_file,
                    model=model,
                    base_url=base_url,
                    reasoning_effort=reasoning_effort,
                    timeout_seconds=timeout_seconds,
                )
        except TimeoutError:
            result = {
                "id": scenario.id,
                "title": scenario.title,
                "status": "failed",
                "failures": [f"evaluation timed out after {timeout_seconds} seconds"],
            }
        except Exception as exc:
            result = {
                "id": scenario.id,
                "title": scenario.title,
                "status": "failed",
                "failures": [f"evaluation harness error: {type(exc).__name__}: {exc}"],
            }
        results.append(result)
        print(
            f"[{scenario.id}] {str(result['status']).upper()}",
            file=sys.stderr,
            flush=True,
        )
    return {
        "schema_version": "traceforge.real-model-evaluation.v1",
        "overall": "passed" if all(item["status"] == "passed" for item in results) else "failed",
        "model": model,
        "base_url": base_url,
        "reasoning_effort": reasoning_effort.value,
        "credential_source": "owner-only file reference",
        "scenarios": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-file", type=Path)
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", DEFAULT_MODEL))
    parser.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument(
        "--reasoning-effort",
        type=ReasoningEffort,
        choices=tuple(ReasoningEffort),
        default=ReasoningEffort.AUTO,
        help="per-turn reasoning effort; auto omits the provider field",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        choices=tuple(SCENARIO_BY_ID),
        help="run only this scenario; repeat to select several",
    )
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--output", type=Path, help="also write the JSON report to this path")
    parser.add_argument("--list", action="store_true", help="list scenario ids and exit")
    args = parser.parse_args()

    if args.list:
        for scenario in SCENARIOS:
            print(f"{scenario.id}\t{scenario.title}")
        return 0
    if args.credential_file is None:
        parser.error("--credential-file is required unless --list is used")
    if args.timeout_seconds < 1:
        parser.error("--timeout-seconds must be at least 1")
    credential_file = validate_credential_file(args.credential_file)
    selected = (
        tuple(SCENARIO_BY_ID[scenario_id] for scenario_id in args.scenario)
        if args.scenario
        else SCENARIOS
    )
    report = asyncio.run(
        evaluate(
            selected,
            credential_file=credential_file,
            model=args.model,
            base_url=args.base_url,
            reasoning_effort=args.reasoning_effort,
            timeout_seconds=args.timeout_seconds,
        )
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.expanduser().resolve().write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["overall"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

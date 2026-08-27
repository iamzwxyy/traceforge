from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from traceforge.models import PlanGate, TaskPlan

_HIGH_RISK_PATTERN = re.compile(
    r"\b(?:auth(?:entication|orization)?|credential|password|permission|security|secret|"
    r"database|schema|migration|dependency|dependencies|package lock|public api|deploy|"
    r"production|delete|remove|rename|move|billing|payment|encryption)\b",
    re.IGNORECASE,
)
_READ_ONLY_COMMANDS = {"rg", "pwd", "ls", "cat", "head", "tail", "wc"}
_JS_CHECK_SCRIPTS = {"test", "lint", "typecheck", "check"}


def assess_plan_gate(
    task: str,
    plan: TaskPlan,
    *,
    clarification_rounds: int = 0,
) -> PlanGate:
    """Conservatively decide whether a visible plan may enter the fast path."""

    reasons: list[str] = []
    risk: Literal["low", "medium", "high"] = "low"

    if clarification_rounds:
        reasons.append("Material choices were clarified with the user")
        risk = "medium"
    if len(plan.impacted_files) != 1:
        reasons.append(
            "The plan must name exactly one impacted file for automatic approval"
        )
        risk = "medium"
    if len(plan.steps) > 2:
        reasons.append("The plan has more than two implementation steps")
        risk = "medium"
    if len(plan.acceptance_checks) > 2:
        reasons.append("The completion contract has more than two checks")
        risk = "medium"
    if plan.risks:
        reasons.append("The planner identified explicit implementation risks")
        risk = "high"

    searchable = "\n".join(
        [
            task,
            plan.summary,
            *(step.title for step in plan.steps),
            *(step.description for step in plan.steps),
        ]
    )
    if _HIGH_RISK_PATTERN.search(searchable):
        reasons.append("The task touches a sensitive or high-impact engineering area")
        risk = "high"

    unsafe_checks = [
        " ".join(check.command)
        for check in plan.acceptance_checks
        if check.command and not is_routine_check(check.command)
    ]
    if unsafe_checks:
        reasons.append("A planned command is not a recognized local verification check")
        risk = "high"

    if not reasons:
        return PlanGate(
            decision="auto_approved",
            risk="low",
            reasons=["Explicit single-file scope with routine local verification only"],
        )
    return PlanGate(decision="approval_required", risk=risk, reasons=reasons)


def is_routine_check(argv: list[str]) -> bool:
    if not argv:
        return False
    executable = Path(argv[0]).name
    if executable in _READ_ONLY_COMMANDS:
        return True
    if executable == "git":
        return len(argv) > 1 and argv[1] in {
            "status",
            "diff",
            "log",
            "show",
            "grep",
            "ls-files",
        }
    if executable in {"pytest", "ruff", "mypy"}:
        return True
    if executable in {"python", "python3"}:
        return len(argv) > 2 and argv[1:3] == ["-m", "pytest"]
    if executable == "uv":
        return len(argv) > 2 and argv[1] == "run" and Path(argv[2]).name in {
            "pytest",
            "ruff",
            "mypy",
        }
    if executable in {"pnpm", "npm", "yarn"}:
        lowered = {part.casefold() for part in argv[1:]}
        forbidden = {"add", "install", "publish", "deploy", "exec", "dlx"}
        return bool(lowered & _JS_CHECK_SCRIPTS) and not bool(lowered & forbidden)
    if executable == "cargo":
        return len(argv) > 1 and argv[1] in {"test", "check", "clippy"}
    if executable == "go":
        return len(argv) > 1 and argv[1] in {"test", "vet"}
    return False

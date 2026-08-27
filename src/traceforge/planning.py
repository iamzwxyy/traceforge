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
_MAX_FAST_PATH_STEPS = 4
_MAX_FAST_PATH_CHECKS = 4


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
    if len(plan.steps) > _MAX_FAST_PATH_STEPS:
        reasons.append(f"The plan has more than {_MAX_FAST_PATH_STEPS} implementation steps")
        risk = "medium"
    if len(plan.acceptance_checks) > _MAX_FAST_PATH_CHECKS:
        reasons.append(f"The completion contract has more than {_MAX_FAST_PATH_CHECKS} checks")
        risk = "medium"

    searchable = "\n".join(
        [
            task,
            plan.summary,
            *(step.title for step in plan.steps),
            *(step.description for step in plan.steps),
            *plan.risks,
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
    if executable == "pytest":
        return not _contains_unsafe_pytest_flag(argv[1:])
    if executable == "ruff":
        return (
            len(argv) > 1
            and argv[1] == "check"
            and not _contains_ruff_mutation_flag(argv[2:])
        )
    if executable == "mypy":
        return "--install-types" not in argv[1:]
    if executable in {"python", "python3"}:
        return (
            len(argv) > 2
            and argv[1:3] == ["-m", "pytest"]
            and not _contains_unsafe_pytest_flag(argv[3:])
        )
    if executable == "uv" and len(argv) > 2 and argv[1] == "run":
        tool = Path(argv[2]).name
        if tool == "pytest":
            return not _contains_unsafe_pytest_flag(argv[3:])
        if tool == "mypy":
            return "--install-types" not in argv[3:]
        return (
            tool == "ruff"
            and len(argv) > 3
            and argv[3] == "check"
            and not _contains_ruff_mutation_flag(argv[4:])
        )
    if executable in {"pnpm", "npm", "yarn"}:
        lowered = {part.casefold() for part in argv[1:]}
        forbidden = {"add", "install", "publish", "deploy", "exec", "dlx"}
        return bool(lowered & _JS_CHECK_SCRIPTS) and not bool(lowered & forbidden)
    if executable == "cargo":
        return len(argv) > 1 and argv[1] in {"test", "check", "clippy"}
    if executable == "go":
        return len(argv) > 1 and argv[1] in {"test", "vet"}
    return False


def routine_check_family(argv: list[str]) -> str | None:
    """Return a conservative family for sandboxed variants of an approved check."""

    if not argv:
        return None
    executable = Path(argv[0]).name
    if executable in {"python", "python3"}:
        return "python:pytest" if len(argv) > 2 and argv[1:3] == ["-m", "pytest"] else None
    if executable == "pytest":
        return "pytest"
    if executable == "ruff" and len(argv) > 1 and argv[1] == "check":
        return "ruff:check"
    if executable == "mypy":
        return "mypy"
    if executable == "uv" and len(argv) > 2 and argv[1] == "run":
        tool = Path(argv[2]).name
        if tool in {"pytest", "mypy"}:
            return f"uv:{tool}"
        if tool == "ruff" and len(argv) > 3 and argv[3] == "check":
            return "uv:ruff:check"
        return None
    if executable in {"pnpm", "npm", "yarn"}:
        lowered = [part.casefold() for part in argv[1:]]
        forbidden = {"add", "install", "publish", "deploy", "exec", "dlx"}
        if any(part in forbidden for part in lowered):
            return None
        script = next((part for part in lowered if part in _JS_CHECK_SCRIPTS), None)
        return f"{executable}:{script}" if script else None
    if executable == "cargo" and len(argv) > 1 and argv[1] in {"test", "check", "clippy"}:
        return f"cargo:{argv[1]}"
    if executable == "go" and len(argv) > 1 and argv[1] in {"test", "vet"}:
        return f"go:{argv[1]}"
    return None


def is_safe_routine_check_variant(candidate: list[str], accepted: list[str]) -> bool:
    """Allow only non-writing, non-interactive focused Pytest variants."""

    family = routine_check_family(candidate)
    if family is None or family != routine_check_family(accepted):
        return False
    if family not in {"pytest", "python:pytest", "uv:pytest"}:
        return False
    if _contains_unsafe_pytest_flag(_pytest_tail(candidate)):
        return False
    if not _pytest_scope_is_narrower_or_equal(candidate, accepted):
        return False

    remaining = list(_pytest_tail(accepted))
    additions: list[str] = []
    for argument in _pytest_tail(candidate):
        try:
            remaining.remove(argument)
        except ValueError:
            additions.append(argument)
    return all(_safe_pytest_addition(argument) for argument in additions)


def _pytest_tail(argv: list[str]) -> list[str]:
    executable = Path(argv[0]).name
    if executable in {"python", "python3", "uv"}:
        return argv[3:]
    return argv[1:]


def _safe_pytest_addition(argument: str) -> bool:
    if "://" in argument:
        return False
    if not argument.startswith("-"):
        return True
    if re.fullmatch(r"-(?:q+|v+|r[a-zA-Z]*)", argument):
        return True
    if argument in {
        "-x",
        "-s",
        "-k",
        "-m",
        "-W",
        "--exitfirst",
        "--disable-warnings",
        "--strict-markers",
        "--strict-config",
        "--no-header",
        "--no-summary",
        "--lf",
        "--ff",
        "--nf",
    }:
        return True
    return argument.startswith(
        (
            "--maxfail=",
            "--tb=",
            "--color=",
            "--capture=",
            "--durations=",
            "--durations-min=",
        )
    )


def _pytest_scope_is_narrower_or_equal(candidate: list[str], accepted: list[str]) -> bool:
    accepted_tail = _pytest_tail(accepted)
    candidate_tail = _pytest_tail(candidate)
    accepted_selectors = _pytest_selectors(accepted_tail)
    candidate_selectors = _pytest_selectors(candidate_tail)
    selectors_preserved = all(
        any(
            selected == expected
            or selected.startswith(expected.rstrip("/") + "/")
            or selected.startswith(expected + "::")
            for selected in candidate_selectors
        )
        for expected in accepted_selectors
    )
    return selectors_preserved and set(_pytest_constraints(accepted_tail)) <= set(
        _pytest_constraints(candidate_tail)
    )


def _pytest_selectors(arguments: list[str]) -> list[str]:
    selectors: list[str] = []
    skip_value = False
    for argument in arguments:
        if skip_value:
            skip_value = False
            continue
        if argument in {"-k", "-m", "-W"}:
            skip_value = True
            continue
        if not argument.startswith("-"):
            selectors.append(argument)
    return selectors


def _pytest_constraints(arguments: list[str]) -> list[str]:
    constraints: list[str] = []
    for index, argument in enumerate(arguments):
        if argument in {"-k", "-m"} and index + 1 < len(arguments):
            constraints.append(f"{argument}={arguments[index + 1]}")
        elif argument.startswith(("--ignore=", "--ignore-glob=", "--deselect=")):
            constraints.append(argument)
    return constraints


def _contains_ruff_mutation_flag(arguments: list[str]) -> bool:
    return any(
        argument in {"--fix", "--fix-only", "--unsafe-fixes"}
        or argument.startswith(("--fix=", "--fix-only=", "--unsafe-fixes="))
        for argument in arguments
    )


def _contains_unsafe_pytest_flag(arguments: list[str]) -> bool:
    unsafe_exact = {
        "--pdb",
        "--trace",
        "--pastebin",
        "--junitxml",
        "--junit-xml",
        "--html",
        "--basetemp",
        "--cov-report",
        "--cov-append",
        "--snapshot-update",
        "--update-snapshots",
        "--inline-snapshot",
        "--record-mode",
        "--self-contained-html",
    }
    unsafe_prefixes = (
        "--junitxml=",
        "--junit-xml=",
        "--html=",
        "--basetemp=",
        "--inline-snapshot=",
        "--record-mode=",
    )
    for argument in arguments:
        if argument in unsafe_exact or argument.startswith(unsafe_prefixes):
            return True
        if argument.startswith("--cov-report="):
            report = argument.partition("=")[2].partition(":")[0]
            if report in {"html", "xml", "json", "lcov", "annotate"}:
                return True
    return False

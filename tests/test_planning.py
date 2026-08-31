from __future__ import annotations

import pytest
from pydantic import ValidationError

from traceforge.models import AcceptanceCheck, PlanStep, TaskPlan
from traceforge.planning import (
    assess_plan_gate,
    is_routine_check,
    is_safe_routine_check_variant,
    routine_check_family,
)


def _plan(**overrides) -> TaskPlan:
    values = {
        "summary": "Update one display label",
        "steps": [PlanStep(id="edit", title="Update the label")],
        "acceptance_checks": [
            AcceptanceCheck(
                id="test",
                label="Focused tests pass",
                command=["uv", "run", "pytest", "tests/test_label.py", "-q"],
            )
        ],
        "impacted_files": ["src/label.py"],
    }
    values.update(overrides)
    return TaskPlan(**values)


def test_single_file_routine_plan_uses_visible_fast_path() -> None:
    gate = assess_plan_gate("Fix the display label", _plan())

    assert gate.decision == "auto_approved"
    assert gate.risk == "low"
    assert "single-file" in gate.reasons[0]


def test_normal_inspect_fix_verify_detail_does_not_disable_fast_path() -> None:
    plan = _plan(
        steps=[
            PlanStep(id="inspect", title="Inspect current behavior"),
            PlanStep(id="fix", title="Fix the condition"),
            PlanStep(id="test", title="Run focused tests"),
            PlanStep(id="review", title="Review the diff"),
        ],
        risks=["Python booleans are integer subclasses"],
    )

    gate = assess_plan_gate("Reject boolean duration values", plan)

    assert gate.decision == "auto_approved"
    assert gate.risk == "low"


def test_unknown_scope_clarification_and_sensitive_work_require_review() -> None:
    unknown = assess_plan_gate(
        "Review the behavior",
        _plan(impacted_files=[]),
    )
    clarified = assess_plan_gate(
        "Fix the display label",
        _plan(),
        clarification_rounds=1,
    )
    sensitive = assess_plan_gate(
        "Change authentication permissions",
        _plan(risks=["Could affect existing sessions"]),
    )
    sensitive_risk_only = assess_plan_gate(
        "Update one parser condition",
        _plan(risks=["Could expose a credential or secret"]),
    )

    assert unknown.decision == "approval_required" and unknown.risk == "medium"
    assert clarified.decision == "approval_required"
    assert sensitive.decision == "approval_required" and sensitive.risk == "high"
    assert sensitive_risk_only.decision == "approval_required"
    assert sensitive_risk_only.risk == "high"


def test_only_non_mutating_local_checks_are_fast_path_eligible() -> None:
    assert is_routine_check(["pnpm", "--filter", "web", "test", "--run"])
    assert is_routine_check(["python3", "-m", "pytest", "-q"])
    assert is_routine_check(
        ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]
    )
    assert is_routine_check(["ruff", "check", "src"])
    assert not is_routine_check(["python3", "-c", "open('x', 'w').write('bad')"])
    assert not is_routine_check(["python", "-m", "pytest", "--pdb"])
    assert not is_routine_check(
        ["python", "-m", "pytest", "--cov-report=html", "tests"]
    )
    assert not is_routine_check(["ruff", "format", "src"])
    assert not is_routine_check(["ruff", "check", "--fix", "src"])
    assert not is_routine_check(["ruff", "check", "--fix-only", "src"])
    assert not is_routine_check(["uv", "run", "ruff", "check", "--fix"])
    assert not is_routine_check(["mypy", "--install-types", "src"])
    assert not is_routine_check(["pnpm", "install"])
    assert not is_routine_check(["curl", "https://example.com"])


def test_routine_check_family_is_conservative_about_launchers() -> None:
    assert routine_check_family(["python", "-m", "pytest", "-q"]) == "python:pytest"
    assert routine_check_family(
        ["python3", "-m", "pytest", "tests/test_api.py", "-v"]
    ) == "python:pytest"
    assert routine_check_family(
        ["python3", "-m", "unittest", "discover", "-s", "tests"]
    ) == "python:unittest"
    assert routine_check_family(["uv", "run", "pytest", "-q"]) == "uv:pytest"
    assert routine_check_family(["ruff", "check", "src"]) == "ruff:check"
    assert routine_check_family(["ruff", "format", "src"]) is None
    assert routine_check_family(["python", "-c", "print('not a check')"]) is None
    assert routine_check_family(["pnpm", "install"]) is None


def test_only_safe_focused_pytest_variants_share_an_approval() -> None:
    accepted = ["python", "-m", "pytest", "-q"]

    assert is_safe_routine_check_variant(
        ["python3", "-m", "pytest", "-q", "tests/test_api.py::test_health", "-v"],
        accepted,
    )
    assert not is_safe_routine_check_variant(
        ["python", "-m", "pytest", "--pdb"], accepted
    )
    assert not is_safe_routine_check_variant(
        ["python", "-m", "pytest", "--junitxml=report.xml"], accepted
    )
    assert not is_safe_routine_check_variant(
        ["python", "-m", "pytest", "--cov-report=html", "tests"], accepted
    )
    assert not is_safe_routine_check_variant(
        ["uv", "run", "pytest", "-q", "tests/test_api.py"], accepted
    )
    assert not is_safe_routine_check_variant(
        ["ruff", "check", "tests/test_api.py"], ["ruff", "check", "src"]
    )
    assert is_safe_routine_check_variant(
        ["python", "-m", "pytest", "tests/test_api.py::test_health"],
        ["python", "-m", "pytest", "tests/test_api.py"],
    )
    assert not is_safe_routine_check_variant(
        ["python", "-m", "pytest"],
        ["python", "-m", "pytest", "tests/test_api.py"],
    )
    assert not is_safe_routine_check_variant(
        ["python", "-m", "pytest", "tests/test_other.py"],
        ["python", "-m", "pytest", "tests/test_api.py"],
    )
    assert not is_safe_routine_check_variant(
        ["python", "-m", "pytest", "tests"],
        ["python", "-m", "pytest", "tests", "-k", "health"],
    )


@pytest.mark.parametrize(
    "path",
    ["", ".", "../outside.py", "/tmp/outside.py", ".git/config"],
)
def test_plan_rejects_unsafe_impacted_file_paths(path: str) -> None:
    with pytest.raises(ValidationError, match="safe relative path"):
        _plan(impacted_files=[path])

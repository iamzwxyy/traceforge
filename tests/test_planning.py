from __future__ import annotations

import pytest
from pydantic import ValidationError

from traceforge.models import AcceptanceCheck, PlanStep, TaskPlan
from traceforge.planning import assess_plan_gate, is_routine_check


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

    assert unknown.decision == "approval_required" and unknown.risk == "medium"
    assert clarified.decision == "approval_required"
    assert sensitive.decision == "approval_required" and sensitive.risk == "high"


def test_only_non_mutating_local_checks_are_fast_path_eligible() -> None:
    assert is_routine_check(["pnpm", "--filter", "web", "test", "--run"])
    assert is_routine_check(["python3", "-m", "pytest", "-q"])
    assert not is_routine_check(["python3", "-c", "open('x', 'w').write('bad')"])
    assert not is_routine_check(["pnpm", "install"])
    assert not is_routine_check(["curl", "https://example.com"])


@pytest.mark.parametrize(
    "path",
    ["", ".", "../outside.py", "/tmp/outside.py", ".git/config"],
)
def test_plan_rejects_unsafe_impacted_file_paths(path: str) -> None:
    with pytest.raises(ValidationError, match="safe relative path"):
        _plan(impacted_files=[path])

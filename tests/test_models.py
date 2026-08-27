from __future__ import annotations

import pytest
from pydantic import ValidationError

from traceforge.models import (
    ClarificationAnswer,
    ClarificationQuestion,
    QuestionOption,
    TaskPlan,
)


def test_clarification_question_requires_unique_options() -> None:
    with pytest.raises(ValidationError, match="unique"):
        ClarificationQuestion(
            id="stack",
            prompt="Which stack?",
            options=[
                QuestionOption(id="python", label="Python"),
                QuestionOption(id="python", label="Python again"),
            ],
        )


def test_clarification_answer_accepts_custom_text() -> None:
    answer = ClarificationAnswer(question_id="name", custom_text="A custom answer")
    assert answer.option_id is None


def test_clarification_answer_rejects_empty_value() -> None:
    with pytest.raises(ValidationError, match="required"):
        ClarificationAnswer(question_id="name")


def test_plan_materializes_a_localized_markdown_contract_from_structured_fields() -> None:
    plan = TaskPlan.model_validate(
        {
            "summary": "修复缓存隔离问题",
            "approach": "使用租户与资料的复合键。",
            "steps": [{"id": "fix", "title": "修改缓存键"}],
            "acceptance_checks": [{"id": "test", "label": "回归测试通过"}],
            "impacted_files": ["cache.py"],
            "markdown": "# stale model prose",
        }
    )

    assert plan.markdown.startswith("# 实施计划\n")
    assert "## 方案" in plan.markdown
    assert "- `cache.py`" in plan.markdown
    assert "stale model prose" not in plan.markdown

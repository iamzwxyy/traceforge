from __future__ import annotations

import pytest
from pydantic import ValidationError

from traceforge.models import ClarificationAnswer, ClarificationQuestion, QuestionOption


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


from __future__ import annotations

import pytest
from duration_parser import normalize_seconds


@pytest.mark.parametrize("value", [0, 1, 3600])
def test_non_negative_integers_are_preserved(value: int) -> None:
    assert normalize_seconds(value) == value


@pytest.mark.parametrize("value", [-1, -600])
def test_negative_integers_are_rejected(value: int) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        normalize_seconds(value)


@pytest.mark.parametrize("value", ["1", 1.0, None])
def test_non_integer_values_are_rejected(value: object) -> None:
    with pytest.raises(TypeError, match="integer"):
        normalize_seconds(value)


@pytest.mark.parametrize("value", [True, False])
def test_booleans_are_not_durations(value: bool) -> None:
    with pytest.raises(TypeError, match="integer"):
        normalize_seconds(value)

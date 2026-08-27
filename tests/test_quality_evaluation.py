from __future__ import annotations

import json
import subprocess
import sys


def test_quality_evaluator_lists_fixed_corpus() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/evaluate_quality.py", "--list"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == [
        "complete-loop\tComplete evidence loop",
        "human-control\tMode-aware human control",
        "truthful-repair\tTruthful repair and termination",
        "recovery-rollback\tRecovery without duplicated side effects",
        "command-isolation\tCommand isolation with auditable escape",
    ]


def test_quality_evaluator_runs_selected_scenario_as_json() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/evaluate_quality.py",
            "--scenario",
            "recovery-rollback",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    report = json.loads(result.stdout)

    assert report["schema_version"] == "traceforge.quality-corpus.v1"
    assert report["overall"] == "passed"
    assert report["counts"] == {"degraded": 0, "failed": 0, "passed": 1}
    assert report["scenarios"][0]["id"] == "recovery-rollback"
    assert "3 passed" in report["scenarios"][0]["pytest_summary"]

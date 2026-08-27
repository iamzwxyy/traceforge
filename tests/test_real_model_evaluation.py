from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def test_real_model_evaluator_lists_pinned_scenarios() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/evaluate_real_model.py", "--list"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == [
        "single-file-fast-path\tSingle-file repair in default Agent mode",
        "multi-file-review-path\tMulti-file repair behind plan review",
    ]


def test_duration_parser_fixture_has_the_pinned_boolean_failure(tmp_path: Path) -> None:
    fixture = tmp_path / "duration-parser"
    shutil.copytree("evaluation/fixtures/duration-parser", fixture)

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=fixture,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1
    assert "2 failed, 8 passed" in result.stdout


def test_tenant_cache_fixture_starts_green_but_fails_hidden_isolation_check(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "tenant-cache-api"
    shutil.copytree("demo/tenant-cache-api", fixture)
    public_suite = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=fixture,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    hidden_check = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from tenant_cache_api.cache import TenantTTLCache\n"
                "cache = TenantTTLCache(clock=lambda: 10)\n"
                "assert cache.get_or_load('acme', '42', lambda: 'Ada') == 'Ada'\n"
                "assert cache.get_or_load('globex', '42', lambda: 'Grace') == 'Grace'\n"
            ),
        ],
        cwd=fixture,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert public_suite.returncode == 0
    assert hidden_check.returncode != 0

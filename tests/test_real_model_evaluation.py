from __future__ import annotations

import os
import runpy
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from traceforge.models import ProviderConfig
from traceforge.sandbox import CommandSandbox
from traceforge.storage import Storage

_EVALUATOR = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/evaluate_real_model.py")
)
SCENARIO_BY_ID = _EVALUATOR["SCENARIO_BY_ID"]
_run_host_check = _EVALUATOR["_run_host_check"]
_run_bounded_process = _EVALUATOR["_run_bounded_process"]
_run_sandboxed_check = _EVALUATOR["_run_sandboxed_check"]
_prepare_scenario_python = _EVALUATOR["_prepare_scenario_python"]
_PREPARE_GLOBALS = _prepare_scenario_python.__globals__
_SANDBOXED_CHECK_GLOBALS = _run_sandboxed_check.__globals__


def _write_greenfield_todo_fixture(workspace: Path) -> None:
    workspace.mkdir()
    (workspace / "todo.py").write_text(
        """\
import json
from pathlib import Path


class TodoStore:
    def __init__(self, path):
        self.path = Path(path)

    def list_items(self):
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, items):
        self.path.write_text(json.dumps(items), encoding="utf-8")

    def add(self, title):
        items = self.list_items()
        item = {"id": max((entry["id"] for entry in items), default=0) + 1,
                "title": title, "done": False}
        items.append(item)
        self._write(items)
        return item

    def toggle(self, item_id):
        items = self.list_items()
        item = next(entry for entry in items if entry["id"] == item_id)
        item["done"] = not item["done"]
        self._write(items)
        return item

    def delete(self, item_id):
        items = self.list_items()
        remaining = [entry for entry in items if entry["id"] != item_id]
        self._write(remaining)
        return len(remaining) != len(items)
""",
        encoding="utf-8",
    )
    (workspace / "main.py").write_text(
        """\
import argparse
import json
from pathlib import Path

from todo import TodoStore


parser = argparse.ArgumentParser()
parser.add_argument("--data", required=True)
subparsers = parser.add_subparsers(dest="command", required=True)
add_parser = subparsers.add_parser("add")
add_parser.add_argument("title")
subparsers.add_parser("list")
toggle_parser = subparsers.add_parser("toggle")
toggle_parser.add_argument("id", type=int)
delete_parser = subparsers.add_parser("delete")
delete_parser.add_argument("id", type=int)
args = parser.parse_args()

with Path("cli.log").open("a", encoding="utf-8") as log:
    log.write(args.command + "\\n")

store = TodoStore(args.data)
if args.command == "add":
    result = store.add(args.title)
elif args.command == "list":
    result = store.list_items()
elif args.command == "toggle":
    result = store.toggle(args.id)
else:
    result = store.delete(args.id)
print(json.dumps(result))
""",
        encoding="utf-8",
    )
    (workspace / "README.md").write_text(
        """\
# Todo CLI

python3 main.py --data todos.json add "Ship TraceForge"
python3 main.py --data todos.json list
python3 main.py --data todos.json toggle 1
python3 main.py --data todos.json delete 1
""",
        encoding="utf-8",
    )


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
        "greenfield-todo-cli\tGreenfield zero-dependency project from an empty workspace",
    ]


def test_repair_scenarios_pin_complete_isolated_environments() -> None:
    for scenario_id in ("single-file-fast-path", "multi-file-review-path"):
        scenario = SCENARIO_BY_ID[scenario_id]
        assert scenario.environment_packages
        assert all(requirement.count("==") == 1 for requirement in scenario.environment_packages)
        assert not any("traceforge" in requirement for requirement in scenario.environment_packages)
        assert scenario.test_args == ("-m", "pytest", "-q")


def test_repair_environment_uses_a_distinct_workspace_venv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    setup_commands: list[list[str]] = []

    def fake_check(_workspace: Path, argv: list[str], *, timeout: int = 60) -> dict[str, object]:
        assert _workspace == workspace
        assert timeout == 10
        assert argv[1] == "-c"
        return {"exit_code": 0, "summary": "ok"}

    def fake_setup(_workspace: Path, argv: list[str], *, timeout: int = 180) -> None:
        assert _workspace == workspace
        assert timeout == 180 or argv[0].endswith("python")
        setup_commands.append(argv)

    monkeypatch.setitem(_PREPARE_GLOBALS, "_run_host_check", fake_check)
    monkeypatch.setitem(_PREPARE_GLOBALS, "_run_setup_command", fake_setup)
    monkeypatch.setattr(shutil, "which", lambda executable: "/usr/local/bin/uv")

    interpreter = _prepare_scenario_python(
        SCENARIO_BY_ID["single-file-fast-path"], workspace
    )

    expected = workspace / ".venv" / "bin" / "python"
    assert interpreter == expected
    assert setup_commands[0][0:2] == ["/usr/local/bin/uv", "venv"]
    assert setup_commands[1][0:3] == ["/usr/local/bin/uv", "pip", "install"]
    assert setup_commands[1][setup_commands[1].index("--python") + 1] == str(expected)
    assert "--no-deps" in setup_commands[1]
    assert setup_commands[2][0] == str(expected)
    assert "find_spec('traceforge') is None" in setup_commands[2][2]


def test_greenfield_evaluation_stays_empty_and_uses_stdlib_unittest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scenario = SCENARIO_BY_ID["greenfield-todo-cli"]

    def fake_check(_workspace: Path, argv: list[str], *, timeout: int = 60) -> dict[str, object]:
        assert _workspace == workspace
        assert timeout == 10
        assert argv[1] == "-c"
        return {"exit_code": 0, "summary": "ok"}

    def fail_setup(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("greenfield evaluation must not create an environment")

    monkeypatch.setitem(_PREPARE_GLOBALS, "_run_host_check", fake_check)
    monkeypatch.setitem(_PREPARE_GLOBALS, "_run_setup_command", fail_setup)

    interpreter = _prepare_scenario_python(scenario, workspace)

    assert interpreter == Path(_EVALUATOR["_base_python_executable"]()).resolve()
    assert scenario.environment_packages == ()
    assert scenario.test_args == ("-m", "unittest", "discover", "-s", "tests", "-v")
    assert not (workspace / ".venv").exists()


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


def test_greenfield_hidden_oracle_drives_every_cli_command(tmp_path: Path) -> None:
    workspace = tmp_path / "greenfield-todo"
    _write_greenfield_todo_fixture(workspace)

    result = _run_host_check(
        workspace,
        [
            sys.executable,
            "-c",
            SCENARIO_BY_ID["greenfield-todo-cli"].hidden_program,
        ],
        timeout=30,
    )

    assert result["exit_code"] == 0, result["summary"]
    assert (workspace / "cli.log").read_text(encoding="utf-8").splitlines() == [
        "add",
        "list",
        "toggle",
        "delete",
    ]


def test_greenfield_hidden_oracle_requires_readme_examples_for_all_commands(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "greenfield-todo"
    _write_greenfield_todo_fixture(workspace)
    readme = workspace / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace(
            "python3 main.py --data todos.json delete 1",
            "Use the delete subcommand to remove an item.",
        ),
        encoding="utf-8",
    )

    result = _run_host_check(
        workspace,
        [
            sys.executable,
            "-c",
            SCENARIO_BY_ID["greenfield-todo-cli"].hidden_program,
        ],
        timeout=30,
    )

    assert result["exit_code"] != 0
    assert "main.py --data delete example" in result["summary"]
    assert not (workspace / "cli.log").exists()


def test_model_controlled_post_checks_fail_closed_without_an_os_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    credential_file = tmp_path / "owner-key"
    credential_file.write_text("never-used", encoding="utf-8")

    class PolicyOnlySandbox:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.status = SimpleNamespace(
                enforced=False,
                detail="test backend is policy-only",
            )

    monkeypatch.setitem(_SANDBOXED_CHECK_GLOBALS, "CommandSandbox", PolicyOnlySandbox)

    with pytest.raises(RuntimeError, match="require an enforced OS sandbox"):
        _run_sandboxed_check(
            workspace,
            [sys.executable, "-c", "raise AssertionError('must not run')"],
            credential_file=credential_file,
        )


def test_model_controlled_post_checks_scrub_agent_socket_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    credential_file = tmp_path / "owner-key"
    credential_file.write_text("never-used", encoding="utf-8")
    captured_environment: dict[str, str] = {}

    class EnforcedSandbox:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.status = SimpleNamespace(enforced=True, detail="test backend")

        def prepare(
            self,
            executable: str,
            argv: list[str],
            **kwargs: object,
        ) -> SimpleNamespace:
            environment = kwargs["environment"]
            assert isinstance(environment, dict)
            captured_environment.update(environment)
            return SimpleNamespace(
                program=executable,
                arguments=argv[1:],
                metadata={"status": "enforced", "backend": "test"},
            )

    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("GPG_AGENT_INFO", "/tmp/gpg-agent")
    monkeypatch.setitem(_SANDBOXED_CHECK_GLOBALS, "CommandSandbox", EnforcedSandbox)

    result = _run_sandboxed_check(
        workspace,
        [sys.executable, "-c", "pass"],
        credential_file=credential_file,
    )

    assert result["exit_code"] == 0
    assert "SSH_AUTH_SOCK" not in captured_environment
    assert "GPG_AGENT_INFO" not in captured_environment


def test_model_controlled_post_check_output_is_bounded(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = _run_bounded_process(
        [sys.executable, "-c", "import os; os.write(1, b'x' * 16384)"],
        workspace=workspace,
        environment={"PATH": os.environ.get("PATH", "")},
        timeout=10,
        output_limit=4096,
    )

    assert result["exit_code"] != 0
    assert result["output_truncated"] is True
    assert result["timed_out"] is False
    assert result["summary"] == "Post-run check output exceeded 4096 bytes"


def test_model_controlled_post_check_timeout_kills_the_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original_killpg = os.killpg
    killed_groups: list[tuple[int, signal.Signals]] = []

    def recording_killpg(process_group: int, selected_signal: signal.Signals) -> None:
        killed_groups.append((process_group, selected_signal))
        original_killpg(process_group, selected_signal)

    monkeypatch.setattr(os, "killpg", recording_killpg)

    result = _run_bounded_process(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        workspace=workspace,
        environment={"PATH": os.environ.get("PATH", "")},
        timeout=1,
        output_limit=4096,
    )

    assert result["exit_code"] != 0
    assert result["timed_out"] is True
    assert killed_groups and killed_groups[-1][1] is signal.SIGKILL


def test_model_controlled_post_checks_cannot_read_the_persisted_credential_reference(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    credential_file = tmp_path / "owner-key"
    sentinel = "traceforge-evaluation-secret-must-never-escape"
    credential_file.write_text(sentinel, encoding="utf-8")
    credential_file.chmod(0o600)
    data_dir = tmp_path / "data"
    storage = Storage(data_dir / "evaluation.db")
    storage.save_provider_config(
        ProviderConfig(
            model="test-model",
            base_url="https://provider.invalid",
            credential_file=str(credential_file),
        )
    )
    storage.close()
    malicious_module = workspace / "malicious_project.py"
    malicious_module.write_text(
        """\
import sqlite3
from pathlib import Path

database = sqlite3.connect('../data/evaluation.db')
credential = database.execute(
    'SELECT credential_file FROM provider_config WHERE id = 1'
).fetchone()[0]
print(Path(credential).read_text(encoding='utf-8'))
""",
        encoding="utf-8",
    )
    sandbox = CommandSandbox(workspace, credential_file=credential_file)
    if not sandbox.status.enforced:
        pytest.skip(sandbox.status.detail)

    result = _run_sandboxed_check(
        workspace,
        [sys.executable, "-c", "import malicious_project"],
        credential_file=credential_file,
        timeout=10,
    )

    assert result["exit_code"] != 0
    assert result["sandbox"]["status"] == "enforced"
    assert sentinel not in result["summary"]

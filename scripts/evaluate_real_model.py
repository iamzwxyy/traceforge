#!/usr/bin/env python3
"""Run low-frequency, credentialed TraceForge acceptance scenarios."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

from traceforge.agent import PlanDecision
from traceforge.config import Settings
from traceforge.demo import DEMO_TASK
from traceforge.events import EventBroker
from traceforge.models import (
    ApprovalMode,
    ClarificationAnswer,
    InteractionMode,
    ProviderConfig,
    ReasoningEffort,
    RunState,
)
from traceforge.proof import build_proof_pack
from traceforge.runtime import AgentRuntime, validate_credential_file
from traceforge.sandbox import CommandSandbox
from traceforge.storage import Storage
from traceforge.tools import _command_environment, scrubbed_environment

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "deepseek-v4-flash-vision-exp"
DEFAULT_BASE_URL = "https://api.deepseek.com"
POST_CHECK_OUTPUT_LIMIT = 1024 * 1024
GREENFIELD_TODO_HIDDEN_PROGRAM = (
    "import re\n"
    "import subprocess\n"
    "import sys\n"
    "from pathlib import Path\n"
    "from tempfile import TemporaryDirectory\n"
    "from todo import TodoStore\n"
    "workspace = Path.cwd()\n"
    "readme_lines = [line.strip().strip('`').lstrip('$ ').lower() "
    "for line in (workspace / 'README.md').read_text(encoding='utf-8').splitlines()]\n"
    "launcher = re.compile(r'(?<![\\w.-])python(?:3(?:\\.12)?)?\\s+(?:\\./)?main\\.py\\b')\n"
    "commands = ('add', 'list', 'toggle', 'delete')\n"
    "def mentions(line, command):\n"
    "    return re.search(rf'(?<![\\w-]){command}(?![\\w-])', line) is not None\n"
    "for command in commands:\n"
    "    examples = [line for line in readme_lines "
    "if launcher.search(line) and '--data' in line and mentions(line, command) "
    "and sum(mentions(line, candidate) for candidate in commands) == 1]\n"
    "    assert examples, "
    "f'README.md lacks a standalone executable main.py --data {command} example'\n"
    "with TemporaryDirectory() as directory:\n"
    "    path = Path(directory) / 'todos.json'\n"
    "    def run_cli(command, *arguments):\n"
    "        completed = subprocess.run(\n"
    "            [sys.executable, 'main.py', '--data', str(path), command, "
    "*map(str, arguments)],\n"
    "            cwd=workspace, capture_output=True, text=True, check=False, timeout=5,\n"
    "        )\n"
    "        assert completed.returncode == 0, "
    "f'{command} failed: {completed.stdout}\\n{completed.stderr}'\n"
    "        return completed\n"
    "    run_cli('add', 'Ship TraceForge')\n"
    "    items = TodoStore(path).list_items()\n"
    "    assert len(items) == 1\n"
    "    first = items[0]\n"
    "    assert {'id', 'title', 'done'} <= first.keys()\n"
    "    assert first['title'] == 'Ship TraceForge' and first['done'] is False\n"
    "    listed = run_cli('list')\n"
    "    assert 'Ship TraceForge' in listed.stdout\n"
    "    run_cli('toggle', first['id'])\n"
    "    toggled = TodoStore(path).list_items()\n"
    "    assert len(toggled) == 1 and toggled[0]['done'] is True\n"
    "    run_cli('delete', first['id'])\n"
    "    assert TodoStore(path).list_items() == []\n"
)


@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    title: str
    fixture: Path | None
    task: str
    mode: InteractionMode
    approval_mode: ApprovalMode
    expected_gate: Literal["auto_approved", "approval_required"]
    required_files: tuple[str, ...]
    allowed_files: tuple[str, ...] | None
    test_args: tuple[str, ...]
    baseline_test_exit: int
    environment_packages: tuple[str, ...]
    hidden_program: str


SCENARIOS = (
    Scenario(
        id="single-file-fast-path",
        title="Single-file repair through the automatic plan gate",
        fixture=ROOT / "evaluation/fixtures/duration-parser",
        task=(
            "Fix the boolean-input bug described in README.md. Preserve normalize_seconds's "
            "public signature and all existing integer behavior. Only modify duration_parser.py; "
            "do not edit tests. Run the full test suite and finish only when it passes."
        ),
        mode=InteractionMode.AGENT,
        approval_mode=ApprovalMode.AUTOMATIC,
        expected_gate="auto_approved",
        required_files=("duration_parser.py",),
        allowed_files=("duration_parser.py",),
        test_args=("-m", "pytest", "-q"),
        baseline_test_exit=1,
        environment_packages=(
            "iniconfig==2.3.0",
            "packaging==26.3",
            "pluggy==1.6.0",
            "pygments==2.21.0",
            "pytest==8.4.2",
        ),
        hidden_program=(
            "from duration_parser import normalize_seconds\n"
            "for value in (True, False):\n"
            "    try:\n"
            "        normalize_seconds(value)\n"
            "    except TypeError:\n"
            "        continue\n"
            "    raise AssertionError(f'{value!r} was accepted')\n"
            "assert normalize_seconds(0) == 0\n"
            "assert normalize_seconds(3600) == 3600\n"
        ),
    ),
    Scenario(
        id="multi-file-review-path",
        title="Multi-file repair behind plan review",
        fixture=ROOT / "demo/tenant-cache-api",
        task=DEMO_TASK,
        mode=InteractionMode.PLAN,
        approval_mode=ApprovalMode.AUTOMATIC,
        expected_gate="approval_required",
        required_files=("src/tenant_cache_api/cache.py",),
        allowed_files=None,
        test_args=("-m", "pytest", "-q"),
        baseline_test_exit=0,
        environment_packages=(
            "annotated-doc==0.0.5",
            "annotated-types==0.8.0",
            "anyio==4.14.2",
            "certifi==2026.7.22",
            "fastapi==0.141.1",
            "h11==0.16.0",
            "httpcore==1.0.9",
            "httpx==0.28.1",
            "idna==3.19",
            "iniconfig==2.3.0",
            "packaging==26.3",
            "pluggy==1.6.0",
            "pydantic==2.13.4",
            "pydantic-core==2.46.4",
            "pygments==2.21.0",
            "pytest==8.4.2",
            "starlette==1.6.0",
            "typing-extensions==4.16.0",
            "typing-inspection==0.4.4",
        ),
        hidden_program=(
            "from tenant_cache_api.cache import TenantTTLCache\n"
            "cache = TenantTTLCache(clock=lambda: 10)\n"
            "assert cache.get_or_load('acme', '42', lambda: 'Ada') == 'Ada'\n"
            "assert cache.get_or_load('globex', '42', lambda: 'Grace') == 'Grace'\n"
            "assert cache.get_or_load('acme', '42', lambda: 'wrong') == 'Ada'\n"
        ),
    ),
    Scenario(
        id="greenfield-todo-cli",
        title="Greenfield zero-dependency project from an empty workspace",
        fixture=None,
        task=(
            "在空目录中创建一个完整、可运行、零第三方依赖的 Python 3.12 命令行待办项目。"
            "todo.py 必须公开 TodoStore(path: str | Path), 并实现 add(title) -> dict、"
            "list_items() -> list[dict]、toggle(id) -> dict、delete(id) -> bool; 数据以 JSON "
            "持久化且重新实例化后仍可读取。每个待办 dict 必须包含 id、title、done, "
            "其中 done 是 bool。main.py 使用 argparse 提供 add、list、toggle、"
            "delete 子命令和 --data 路径。添加 tests/test_todo.py 和 README.md。"
            "README.md 必须分别用四条可直接复制执行的单行命令展示 python3 main.py、"
            "--data 与 add、list、toggle、delete。"
            "不要使用或安装第三方依赖。验收命令必须是 python3 -m unittest discover -s "
            "tests -v, 并在全部通过后结束。"
        ),
        mode=InteractionMode.AGENT,
        approval_mode=ApprovalMode.FULL_ACCESS,
        expected_gate="approval_required",
        required_files=("todo.py", "main.py", "tests/test_todo.py", "README.md"),
        allowed_files=None,
        test_args=("-m", "unittest", "discover", "-s", "tests", "-v"),
        baseline_test_exit=1,
        environment_packages=(),
        hidden_program=GREENFIELD_TODO_HIDDEN_PROGRAM,
    ),
)
SCENARIO_BY_ID = {scenario.id: scenario for scenario in SCENARIOS}


def _copy_fixture(source: Path | None, destination: Path) -> None:
    if source is None:
        destination.mkdir(parents=True)
        return
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "uv.lock", "__pycache__", ".pytest_cache"
        ),
    )


def _host_environment(workspace: Path) -> dict[str, str]:
    environment = scrubbed_environment()
    environment.pop("VIRTUAL_ENV", None)
    environment.pop("VIRTUAL_ENV_PROMPT", None)
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment.pop("__PYVENV_LAUNCHER__", None)
    source_root = workspace / "src"
    if source_root.is_dir():
        environment["PYTHONPATH"] = str(source_root)
    return environment


def _run_host_check(workspace: Path, argv: list[str], *, timeout: int = 60) -> dict[str, Any]:
    completed = subprocess.run(
        argv,
        cwd=workspace,
        env=_host_environment(workspace),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return {
        "exit_code": completed.returncode,
        "summary": lines[-1] if lines else "No output",
    }


def _require_enforced_check_sandbox(
    workspace: Path, credential_file: Path
) -> CommandSandbox:
    sandbox = CommandSandbox(workspace, credential_file=credential_file)
    if not sandbox.status.enforced:
        raise RuntimeError(
            "real-model post-run checks require an enforced OS sandbox: "
            + sandbox.status.detail
        )
    return sandbox


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


def _run_bounded_process(
    argv: list[str],
    *,
    workspace: Path,
    environment: dict[str, str],
    timeout: int,
    output_limit: int = POST_CHECK_OUTPUT_LIMIT,
) -> dict[str, Any]:
    process = subprocess.Popen(
        argv,
        cwd=workspace,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    stdout = process.stdout
    assert stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(stdout, selectors.EVENT_READ)
    chunks: list[bytes] = []
    stored_size = 0
    truncated = False
    timed_out = False
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _kill_process_group(process)
                break
            for key, _mask in selector.select(min(remaining, 0.25)):
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                available = output_limit - stored_size
                if available > 0:
                    chunks.append(chunk[:available])
                    stored_size += min(len(chunk), available)
                if len(chunk) > available:
                    truncated = True
                    _kill_process_group(process)
                    break
            if truncated:
                break
    except BaseException:
        _kill_process_group(process)
        raise
    finally:
        selector.close()
        stdout.close()
    if process.poll() is None:
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_group(process)
    output = b"".join(chunks).decode("utf-8", errors="replace").strip()
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if timed_out:
        summary = f"Post-run check timed out after {timeout} seconds"
    elif truncated:
        summary = f"Post-run check output exceeded {output_limit} bytes"
    else:
        summary = lines[-1] if lines else "No output"
    exit_code = process.returncode if process.returncode is not None else 1
    if (timed_out or truncated) and exit_code == 0:
        exit_code = 1
    return {
        "exit_code": exit_code,
        "summary": summary,
        "timed_out": timed_out,
        "output_truncated": truncated,
    }


def _run_sandboxed_check(
    workspace: Path,
    argv: list[str],
    *,
    credential_file: Path,
    timeout: int = 60,
) -> dict[str, Any]:
    """Run model-controlled post-checks only inside an enforced OS sandbox."""

    sandbox = _require_enforced_check_sandbox(workspace, credential_file)
    with TemporaryDirectory(prefix="traceforge-evaluation-check-") as temporary:
        command_temp = Path(temporary).resolve()
        sandbox_home = command_temp / "home"
        sandbox_tmp = command_temp / "tmp"
        sandbox_cache = command_temp / "cache"
        for directory in (sandbox_home, sandbox_tmp, sandbox_cache):
            directory.mkdir()
        environment = _command_environment(
            workspace,
            home=sandbox_home,
            temp=sandbox_tmp,
            cache=sandbox_cache,
        )
        source_root = workspace / "src"
        if source_root.is_dir():
            environment["PYTHONPATH"] = str(source_root)
        executable = (
            argv[0]
            if Path(argv[0]).is_absolute()
            else shutil.which(argv[0], path=environment["PATH"])
        )
        if executable is None:
            raise RuntimeError(f"post-run check executable not found: {argv[0]}")
        launch = sandbox.prepare(
            executable,
            argv,
            cwd=workspace,
            command_temp=command_temp,
            environment=environment,
            bypass=False,
        )
        result = _run_bounded_process(
            [launch.program, *launch.arguments],
            workspace=workspace,
            environment=environment,
            timeout=timeout,
        )
    return {**result, "sandbox": launch.metadata}


def _run_setup_command(workspace: Path, argv: list[str], *, timeout: int = 180) -> None:
    result = _run_host_check(workspace, argv, timeout=timeout)
    if result["exit_code"] != 0:
        raise RuntimeError(f"evaluation environment setup failed: {result['summary']}")


def _base_python_executable() -> Path:
    candidate = getattr(sys, "_base_executable", None)
    if not isinstance(candidate, str) or not candidate:
        candidate = sys.executable
    return Path(candidate).resolve()


def _prepare_scenario_python(scenario: Scenario, workspace: Path) -> Path:
    base_python = _base_python_executable()
    version = _run_host_check(
        workspace,
        [
            str(base_python),
            "-c",
            "import sys; raise SystemExit(sys.version_info[:2] != (3, 12))",
        ],
        timeout=10,
    )
    if version["exit_code"] != 0:
        raise RuntimeError("real-model evaluation requires the safe base Python 3.12 runtime")
    if not scenario.environment_packages:
        return base_python

    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("real-model repair evaluation requires uv to create an isolated venv")
    environment_root = workspace / ".venv"
    interpreter = environment_root / "bin" / "python"
    _run_setup_command(
        workspace,
        [
            uv,
            "venv",
            "--python",
            str(base_python),
            "--no-python-downloads",
            str(environment_root),
        ],
    )
    _run_setup_command(
        workspace,
        [
            uv,
            "pip",
            "install",
            "--python",
            str(interpreter),
            "--no-deps",
            "--no-progress",
            *scenario.environment_packages,
        ],
    )
    _run_setup_command(
        workspace,
        [
            str(interpreter),
            "-c",
            (
                "import importlib.util, pathlib, sys; "
                "assert sys.version_info[:2] == (3, 12); "
                "assert pathlib.Path(sys.prefix).resolve() == pathlib.Path('.venv').resolve(); "
                "assert importlib.util.find_spec('traceforge') is None"
            ),
        ],
        timeout=10,
    )
    # Keep the visible venv launcher path. Resolving its symlink would invoke the base
    # interpreter directly and discard the workspace prefix and installed packages.
    return interpreter


def _scenario_failures(
    scenario: Scenario,
    *,
    state: RunState,
    gate: str | None,
    changed_files: list[str],
    checks_fresh: bool,
    verdict: str | None,
    proof_status: str,
    action_prompts: list[dict[str, Any]],
    baseline_tests: dict[str, Any],
    baseline_hidden: dict[str, Any],
    independent_tests: dict[str, Any],
    hidden_check: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    if baseline_tests["exit_code"] != scenario.baseline_test_exit:
        failures.append("fixture test baseline did not match the pinned precondition")
    if baseline_hidden["exit_code"] == 0:
        failures.append("hidden semantic check did not fail before the repair")
    if state is not RunState.SUCCEEDED:
        failures.append(f"run ended in {state.value}, not succeeded")
    if gate != scenario.expected_gate:
        failures.append(f"plan gate was {gate}, expected {scenario.expected_gate}")
    missing = sorted(set(scenario.required_files) - set(changed_files))
    if missing:
        failures.append("required changed files missing: " + ", ".join(missing))
    if scenario.allowed_files is not None:
        unexpected = sorted(set(changed_files) - set(scenario.allowed_files))
        if unexpected:
            failures.append("unexpected changed files: " + ", ".join(unexpected))
    else:
        if not any(path.startswith("tests/") for path in changed_files):
            failures.append("multi-file scenario did not add or update a regression test")
    if not checks_fresh:
        failures.append("planned acceptance checks were stale")
    if verdict != "pass":
        failures.append(f"independent verifier verdict was {verdict}")
    if proof_status != "proven":
        failures.append(f"Proof Pack status was {proof_status}")
    if action_prompts:
        failures.append(f"encountered {len(action_prompts)} unplanned action approval(s)")
    if independent_tests["exit_code"] != 0:
        failures.append("independent full test run failed")
    if hidden_check["exit_code"] != 0:
        failures.append("independent hidden semantic check failed")
    return failures


async def _drive_run(
    scenario: Scenario,
    workspace: Path,
    *,
    credential_file: Path,
    model: str,
    base_url: str,
    reasoning_effort: ReasoningEffort,
    timeout_seconds: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    scenario_python = _prepare_scenario_python(scenario, workspace)
    _require_enforced_check_sandbox(workspace, credential_file)
    test_argv = [str(scenario_python), *scenario.test_args]
    baseline_tests = _run_host_check(workspace, test_argv)
    baseline_hidden = _run_host_check(
        workspace, [str(scenario_python), "-c", scenario.hidden_program], timeout=10
    )
    data_dir = workspace.parent / "data"
    settings = Settings(
        workspace=workspace,
        data_dir=data_dir,
        api_key="",
        base_url=None,
        model=model,
    )
    storage = Storage(data_dir / "evaluation.db")
    storage.save_provider_config(
        ProviderConfig(
            model=model,
            base_url=base_url,
            credential_file=str(credential_file),
        )
    )
    runtime = AgentRuntime(settings, storage, EventBroker(storage))
    clarification_rounds: list[dict[str, Any]] = []
    plan_reviews: list[dict[str, Any]] = []
    action_prompts: list[dict[str, Any]] = []
    handled_clarifications: set[tuple[int, tuple[str, ...]]] = set()
    handled_approvals: set[str] = set()
    try:
        connection = await runtime.test_connection()
        if not connection["ok"]:
            raise RuntimeError(
                "provider native tool-call probe failed: " + str(connection["detail"])
            )
        run = await runtime.start_run(
            scenario.task,
            workspace,
            mode=scenario.mode,
            approval_mode=scenario.approval_mode,
            reasoning_effort=reasoning_effort,
        )
        manager = runtime.manager_for_run(run.id)
        async with asyncio.timeout(timeout_seconds):
            while True:
                current = storage.get_run(run.id)
                if current.state.terminal or current.state is RunState.INTERRUPTED:
                    break
                if current.state is RunState.AWAITING_CLARIFICATION and current.clarification:
                    key = (
                        current.clarification.round,
                        tuple(question.id for question in current.clarification.questions),
                    )
                    if key not in handled_clarifications:
                        answers: list[ClarificationAnswer] = []
                        choices: list[dict[str, str]] = []
                        for question in current.clarification.questions:
                            option = next(
                                (item for item in question.options if item.recommended),
                                question.options[0],
                            )
                            choices.append({"question": question.prompt, "answer": option.label})
                            answers.append(
                                ClarificationAnswer(
                                    question_id=question.id,
                                    option_id=option.id,
                                )
                            )
                        clarification_rounds.append(
                            {"round": current.clarification.round, "choices": choices}
                        )
                        print(
                            f"[{scenario.id}] selected recommended clarification answers",
                            file=sys.stderr,
                            flush=True,
                        )
                        await manager.answer_clarification(run.id, answers)
                        handled_clarifications.add(key)
                elif current.state is RunState.AWAITING_PLAN_APPROVAL and current.plan:
                    approval_key = current.plan.summary
                    if approval_key not in handled_approvals:
                        plan_reviews.append(
                            {
                                "gate": (
                                    current.plan_gate.decision if current.plan_gate else None
                                ),
                                "risk": current.plan_gate.risk if current.plan_gate else None,
                                "impacted_files": current.plan.impacted_files,
                                "steps": len(current.plan.steps),
                                "checks": [
                                    check.command for check in current.plan.acceptance_checks
                                ],
                            }
                        )
                        print(
                            f"[{scenario.id}] approved the visible evaluation plan",
                            file=sys.stderr,
                            flush=True,
                        )
                        await manager.decide_plan(run.id, PlanDecision(decision="approve"))
                        handled_approvals.add(approval_key)
                elif current.state is RunState.AWAITING_ACTION_APPROVAL:
                    approval = current.pending_approval
                    if approval and approval.id not in handled_approvals:
                        action_prompts.append(
                            {
                                "tool": approval.tool_call.name,
                                "argv": approval.tool_call.arguments.get("argv"),
                                "reason": approval.reason,
                            }
                        )
                        print(
                            f"[{scenario.id}] rejected an unplanned action approval",
                            file=sys.stderr,
                            flush=True,
                        )
                        await manager.decide_action(
                            run.id,
                            approval.id,
                            approved=False,
                        )
                        handled_approvals.add(approval.id)
                await asyncio.sleep(0.05)

        completed = await manager.wait(run.id)
        proof = build_proof_pack(completed, storage)
        independent_tests = _run_sandboxed_check(
            workspace,
            test_argv,
            credential_file=credential_file,
        )
        hidden_check = _run_sandboxed_check(
            workspace,
            [str(scenario_python), "-c", scenario.hidden_program],
            credential_file=credential_file,
            timeout=10,
        )
        gate = completed.plan_gate.decision if completed.plan_gate else None
        verdict = completed.verification.verdict.value if completed.verification else None
        failures = _scenario_failures(
            scenario,
            state=completed.state,
            gate=gate,
            changed_files=proof.changed_files,
            checks_fresh=proof.checks_fresh,
            verdict=verdict,
            proof_status=proof.proof_status,
            action_prompts=action_prompts,
            baseline_tests=baseline_tests,
            baseline_hidden=baseline_hidden,
            independent_tests=independent_tests,
            hidden_check=hidden_check,
        )
        return {
            "id": scenario.id,
            "title": scenario.title,
            "status": "failed" if failures else "passed",
            "duration_seconds": round(time.perf_counter() - started, 3),
            "state": completed.state.value,
            "run_error": completed.error,
            "plan_gate": (
                completed.plan_gate.model_dump(mode="json")
                if completed.plan_gate
                else None
            ),
            "changed_files": proof.changed_files,
            "checks_fresh": proof.checks_fresh,
            "verdict": verdict,
            "proof_status": proof.proof_status,
            "sandbox": proof.command_sandbox.model_dump(mode="json"),
            "steps": completed.step_count,
            "repair_cycles": completed.repair_cycles,
            "event_count": proof.event_count,
            "reasoning_effort": completed.reasoning_effort.value,
            "approval_mode": completed.approval_mode.value,
            "clarification_rounds": clarification_rounds,
            "plan_reviews": plan_reviews,
            "action_prompts": action_prompts,
            "environment": {
                "python": "workspace .venv" if scenario.environment_packages else "base 3.12",
                "packages": list(scenario.environment_packages),
            },
            "baseline_tests": baseline_tests,
            "baseline_hidden": baseline_hidden,
            "independent_tests": independent_tests,
            "hidden_check": hidden_check,
            "failures": failures,
        }
    finally:
        await runtime.shutdown()
        storage.close()


async def evaluate(
    scenarios: tuple[Scenario, ...],
    *,
    credential_file: Path,
    model: str,
    base_url: str,
    reasoning_effort: ReasoningEffort,
    timeout_seconds: int,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for scenario in scenarios:
        print(f"[{scenario.id}] starting", file=sys.stderr, flush=True)
        try:
            with TemporaryDirectory(prefix=f"traceforge-{scenario.id}-") as temporary:
                workspace = Path(temporary) / "workspace"
                _copy_fixture(scenario.fixture, workspace)
                result = await _drive_run(
                    scenario,
                    workspace,
                    credential_file=credential_file,
                    model=model,
                    base_url=base_url,
                    reasoning_effort=reasoning_effort,
                    timeout_seconds=timeout_seconds,
                )
        except TimeoutError:
            result = {
                "id": scenario.id,
                "title": scenario.title,
                "status": "failed",
                "failures": [f"evaluation timed out after {timeout_seconds} seconds"],
            }
        except Exception as exc:
            result = {
                "id": scenario.id,
                "title": scenario.title,
                "status": "failed",
                "failures": [f"evaluation harness error: {type(exc).__name__}: {exc}"],
            }
        results.append(result)
        print(
            f"[{scenario.id}] {str(result['status']).upper()}",
            file=sys.stderr,
            flush=True,
        )
    return {
        "schema_version": "traceforge.real-model-evaluation.v1",
        "overall": "passed" if all(item["status"] == "passed" for item in results) else "failed",
        "model": model,
        "base_url": base_url,
        "reasoning_effort": reasoning_effort.value,
        "credential_source": "owner-only file reference",
        "scenarios": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-file", type=Path)
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", DEFAULT_MODEL))
    parser.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument(
        "--reasoning-effort",
        type=ReasoningEffort,
        choices=tuple(ReasoningEffort),
        default=ReasoningEffort.AUTO,
        help="per-turn reasoning effort; auto omits the provider field",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        choices=tuple(SCENARIO_BY_ID),
        help="run only this scenario; repeat to select several",
    )
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--output", type=Path, help="also write the JSON report to this path")
    parser.add_argument("--list", action="store_true", help="list scenario ids and exit")
    args = parser.parse_args()

    if args.list:
        for scenario in SCENARIOS:
            print(f"{scenario.id}\t{scenario.title}")
        return 0
    if args.credential_file is None:
        parser.error("--credential-file is required unless --list is used")
    if args.timeout_seconds < 1:
        parser.error("--timeout-seconds must be at least 1")
    credential_file = validate_credential_file(args.credential_file)
    selected = (
        tuple(SCENARIO_BY_ID[scenario_id] for scenario_id in args.scenario)
        if args.scenario
        else SCENARIOS
    )
    report = asyncio.run(
        evaluate(
            selected,
            credential_file=credential_file,
            model=args.model,
            base_url=args.base_url,
            reasoning_effort=args.reasoning_effort,
            timeout_seconds=args.timeout_seconds,
        )
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.expanduser().resolve().write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["overall"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

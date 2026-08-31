from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from traceforge.agent import AgentManager, PlanDecision
from traceforge.config import Settings
from traceforge.demo import DEMO_TASK, scripted_demo_provider
from traceforge.models import ClarificationAnswer, InteractionMode, RunState, Verdict
from traceforge.storage import Storage


async def _wait_for_state(storage: Storage, run_id: str, state: RunState) -> None:
    async with asyncio.timeout(3):
        while storage.get_run(run_id).state is not state:  # noqa: ASYNC110
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_scripted_demo_completes_with_real_test_evidence(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "demo" / "tenant-cache-api"
    workspace = tmp_path / "tenant-cache-api"
    shutil.copytree(source, workspace, ignore=shutil.ignore_patterns(".venv", "uv.lock"))
    settings = Settings(
        workspace=workspace,
        data_dir=tmp_path / "data",
        api_key="",
        base_url=None,
        model="scripted-demo",
        suggested_task=DEMO_TASK,
        demo_mode=True,
    )
    storage = Storage(settings.data_dir / "test.db")
    manager = AgentManager(settings, storage, scripted_demo_provider())

    try:
        run = await manager.start_run(DEMO_TASK, mode=InteractionMode.PLAN)
        await _wait_for_state(storage, run.id, RunState.AWAITING_CLARIFICATION)
        await manager.answer_clarification(
            run.id,
            [
                ClarificationAnswer(
                    question_id="compatibility", option_id="preserve"
                )
            ],
        )
        await _wait_for_state(storage, run.id, RunState.AWAITING_PLAN_APPROVAL)
        await manager.decide_plan(run.id, PlanDecision(decision="approve"))

        completed = await manager.wait(run.id)

        assert completed.state is RunState.SUCCEEDED
        assert completed.verification is not None
        assert completed.verification.verdict is Verdict.PASS
        assert completed.plan is not None
        assert completed.plan.acceptance_checks[0].exit_code == 0
        assert all(step.status == "completed" for step in completed.plan.steps)
        assert (workspace / "tests" / "test_tenant_isolation.py").is_file()
        cache_source = (workspace / "src" / "tenant_cache_api" / "cache.py").read_text()
        assert "cache_key = (tenant_id, profile_id)" in cache_source
    finally:
        await manager.shutdown()
        storage.close()

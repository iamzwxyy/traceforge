from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from traceforge import __version__
from traceforge.agent import AgentManager, InvalidRunAction, PlanDecision, RunConflictError
from traceforge.config import Settings
from traceforge.events import EventBroker
from traceforge.models import (
    ApprovalRequest,
    ClarificationAnswer,
    ClarificationRequest,
    RunEvent,
    RunRecord,
    RunState,
    TaskPlan,
    VerificationReport,
)
from traceforge.provider import ModelProvider, OpenAICompatibleProvider
from traceforge.storage import Storage


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=20_000)
    verifier_enabled: bool = True


class ClarificationAnswersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: list[ClarificationAnswer] = Field(min_length=1, max_length=3)


class ActionDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool


class RunView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    task: str
    workspace: str
    state: RunState
    verifier_enabled: bool
    plan: TaskPlan | None
    clarification: ClarificationRequest | None
    pending_approval: ApprovalRequest | None
    verification: VerificationReport | None
    step_count: int
    repair_cycles: int
    error: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, run: RunRecord) -> RunView:
        public = run.model_dump(exclude={"messages", "plan_approved", "interrupted_from"})
        return cls.model_validate(public)


def create_app(settings: Settings, *, provider: ModelProvider | None = None) -> FastAPI:
    storage = Storage(settings.data_dir / "traceforge.db")
    broker = EventBroker(storage)
    manager = AgentManager(
        settings,
        storage,
        provider or OpenAICompatibleProvider(settings),
        broker=broker,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await manager.shutdown()
        storage.close()

    app = FastAPI(
        title="TraceForge API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.storage = storage
    app.state.manager = manager
    app.state.broker = broker

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self' ws: wss:; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'"
        )
        return response

    @app.exception_handler(KeyError)
    async def key_error_handler(_request: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc).strip("'")})

    @app.exception_handler(InvalidRunAction)
    async def invalid_action_handler(_request: Request, exc: InvalidRunAction) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(RunConflictError)
    async def run_conflict_handler(_request: Request, exc: RunConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/status")
    async def get_status() -> dict[str, Any]:
        return {
            "version": __version__,
            "workspace": str(settings.workspace),
            "model": settings.model,
            "base_url": settings.masked_base_url,
            "api_key_configured": bool(settings.api_key),
            "limits": {
                "context": settings.context_limit,
                "steps": settings.max_steps,
                "repair_cycles": settings.max_repair_cycles,
            },
        }

    @app.get("/api/runs", response_model=list[RunView])
    async def list_runs() -> list[RunView]:
        return [RunView.from_record(run) for run in storage.list_runs(settings.workspace)]

    @app.post("/api/runs", response_model=RunView, status_code=status.HTTP_201_CREATED)
    async def create_run(body: CreateRunRequest) -> RunView:
        run = await manager.start_run(body.task, verifier_enabled=body.verifier_enabled)
        return RunView.from_record(run)

    @app.get("/api/runs/{run_id}", response_model=RunView)
    async def get_run(run_id: str) -> RunView:
        return RunView.from_record(storage.get_run(run_id))

    @app.get("/api/runs/{run_id}/events", response_model=list[RunEvent])
    async def get_events(
        run_id: str,
        after_seq: Annotated[int, Query(ge=0)] = 0,
    ) -> list[RunEvent]:
        storage.get_run(run_id)
        return storage.get_events(run_id, after_seq=after_seq)

    @app.get("/api/runs/{run_id}/diff")
    async def get_diff(run_id: str) -> dict[str, str]:
        storage.get_run(run_id)
        return {"diff": manager.workspace.diff(run_id)}

    @app.post("/api/runs/{run_id}/answers", status_code=status.HTTP_202_ACCEPTED)
    async def answer_questions(
        run_id: str, body: ClarificationAnswersRequest
    ) -> dict[str, bool]:
        await manager.answer_clarification(run_id, body.answers)
        return {"accepted": True}

    @app.post("/api/runs/{run_id}/plan-decision", status_code=status.HTTP_202_ACCEPTED)
    async def decide_plan(run_id: str, body: PlanDecision) -> dict[str, bool]:
        await manager.decide_plan(run_id, body)
        return {"accepted": True}

    @app.post(
        "/api/runs/{run_id}/actions/{approval_id}/decision",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def decide_action(
        run_id: str, approval_id: str, body: ActionDecisionRequest
    ) -> dict[str, bool]:
        run = storage.get_run(run_id)
        if run.pending_approval is None or run.pending_approval.id != approval_id:
            raise HTTPException(status_code=409, detail="Approval is no longer pending")
        await manager.decide_action(run_id, approved=body.approved)
        return {"accepted": True}

    @app.post("/api/runs/{run_id}/cancel", response_model=RunView)
    async def cancel_run(run_id: str) -> RunView:
        return RunView.from_record(await manager.cancel(run_id))

    @app.post("/api/runs/{run_id}/resume", response_model=RunView)
    async def resume_run(run_id: str) -> RunView:
        return RunView.from_record(await manager.resume(run_id))

    @app.post("/api/runs/{run_id}/rollback")
    async def rollback_run(run_id: str) -> dict[str, list[str]]:
        result = await manager.rollback(run_id)
        return {
            "restored": result.restored,
            "removed": result.removed,
            "conflicts": result.conflicts,
        }

    @app.websocket("/api/runs/{run_id}/events")
    async def run_events(websocket: WebSocket, run_id: str, after_seq: int = 0) -> None:
        origin = websocket.headers.get("origin")
        if origin and not _allowed_origin(origin):
            await websocket.close(code=1008, reason="Untrusted origin")
            return
        try:
            storage.get_run(run_id)
        except KeyError:
            await websocket.close(code=1008, reason="Run not found")
            return
        await websocket.accept()
        queue = broker.subscribe(run_id)
        last_seq = after_seq
        try:
            for event in storage.get_events(run_id, after_seq=after_seq):
                await websocket.send_json(event.model_dump(mode="json"))
                last_seq = event.seq
            while True:
                event = await queue.get()
                if event.seq <= last_seq:
                    continue
                await websocket.send_json(event.model_dump(mode="json"))
                last_seq = event.seq
        except WebSocketDisconnect:
            pass
        finally:
            broker.unsubscribe(run_id, queue)

    _mount_frontend(app)
    return app


def _allowed_origin(origin: str) -> bool:
    return bool(re.fullmatch(r"https?://(?:127\.0\.0\.1|localhost)(?::\d+)?", origin))


def _mount_frontend(app: FastAPI) -> None:
    static_root = Path(__file__).with_name("static")
    assets = static_root / "assets"
    index = static_root / "index.html"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/", include_in_schema=False)
    async def frontend_index() -> Response:
        if index.is_file():
            return FileResponse(index)
        return JSONResponse(
            status_code=503,
            content={"detail": "Frontend assets are not built. Run pnpm --dir web build."},
        )

    @app.get("/{frontend_path:path}", include_in_schema=False)
    async def frontend_fallback(frontend_path: str) -> FileResponse:
        if frontend_path.startswith(("api/", "healthz")) or not index.is_file():
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(index)

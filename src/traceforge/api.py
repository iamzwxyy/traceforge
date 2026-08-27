from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, MutableMapping
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

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
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator

from traceforge import __version__
from traceforge.agent import InvalidRunAction, PlanDecision, RunConflictError
from traceforge.config import Settings
from traceforge.context import ContextManager
from traceforge.events import EventBroker
from traceforge.models import (
    ApprovalRequest,
    ClarificationAnswer,
    ClarificationRequest,
    PlanGate,
    ProjectRecord,
    ProofPack,
    ProviderConfig,
    RunEvent,
    RunRecord,
    RunState,
    TaskPlan,
    VerificationReport,
)
from traceforge.proof import build_proof_pack, proof_pack_markdown
from traceforge.provider import ModelProvider
from traceforge.runtime import AgentRuntime, resolve_workspace
from traceforge.storage import Storage
from traceforge.workspace import Workspace


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=20_000)
    verifier_enabled: bool = True
    project_id: str | None = None
    workspace: str | None = Field(default=None, max_length=4_096)

    @model_validator(mode="after")
    def validate_target(self) -> CreateRunRequest:
        if self.project_id and self.workspace:
            raise ValueError("Choose either a project or a direct-task workspace, not both")
        return self


class CreateProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    root: str = Field(min_length=1, max_length=4_096)
    create_directory: bool = False


class UpdateProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=200)
    base_url: str | None = Field(default=None, max_length=2_000)
    credential_file: str | None = Field(default=None, max_length=4_096)


class ProviderConfigView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    base_url: str | None
    credential_source: str
    credential_file: str | None
    credential_env: str = "OPENAI_API_KEY"
    api_key_configured: bool
    updated_at: datetime


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
    project_id: str | None
    state: RunState
    verifier_enabled: bool
    plan: TaskPlan | None
    clarification: ClarificationRequest | None
    pending_approval: ApprovalRequest | None
    verification: VerificationReport | None
    plan_gate: PlanGate | None
    step_count: int
    repair_cycles: int
    context_tokens: int
    context_limit: int
    error: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, run: RunRecord, *, context_limit: int) -> RunView:
        public = run.model_dump(exclude={"messages", "plan_approved", "interrupted_from"})
        public["context_tokens"] = ContextManager(context_limit).estimated_tokens(run.messages)
        public["context_limit"] = context_limit
        return cls.model_validate(public)


def create_app(settings: Settings, *, provider: ModelProvider | None = None) -> FastAPI:
    storage = Storage(settings.data_dir / "traceforge.db")
    storage.mark_all_active_runs_interrupted()
    broker = EventBroker(storage)
    runtime = AgentRuntime(
        settings, storage, broker, provider_override=provider
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await runtime.shutdown()
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
    app.state.runtime = runtime
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

    @app.exception_handler(ValueError)
    async def value_error_handler(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/status")
    async def get_status() -> dict[str, Any]:
        config = runtime.provider_config
        last_workspace = storage.get_preference("last_workspace") or str(settings.workspace)
        return {
            "version": __version__,
            "workspace": str(settings.workspace),
            "last_workspace": last_workspace,
            "model": config.model,
            "base_url": config.base_url or "https://api.openai.com/v1",
            "api_key_configured": runtime.credential_configured(config),
            "suggested_task": settings.suggested_task,
            "limits": {
                "context": settings.context_limit,
                "steps": settings.max_steps,
                "repair_cycles": settings.max_repair_cycles,
            },
        }

    def provider_view(config: ProviderConfig | None = None) -> ProviderConfigView:
        selected = config or runtime.provider_config
        if selected.credential_file:
            source = "file"
        elif settings.api_key:
            source = "environment"
        else:
            source = "missing"
        return ProviderConfigView(
            model=selected.model,
            base_url=selected.base_url,
            credential_source=source,
            credential_file=selected.credential_file,
            api_key_configured=runtime.credential_configured(selected),
            updated_at=selected.updated_at,
        )

    @app.get("/api/provider", response_model=ProviderConfigView)
    async def get_provider_config() -> ProviderConfigView:
        return provider_view()

    @app.put("/api/provider", response_model=ProviderConfigView)
    async def update_provider_config(body: UpdateProviderRequest) -> ProviderConfigView:
        saved = await runtime.save_provider_config(
            ProviderConfig(
                model=body.model,
                base_url=body.base_url,
                credential_file=body.credential_file,
            )
        )
        return provider_view(saved)

    @app.post("/api/provider/test")
    async def test_provider_connection() -> dict[str, Any]:
        return await runtime.test_connection()

    @app.get("/api/projects", response_model=list[ProjectRecord])
    async def list_projects() -> list[ProjectRecord]:
        return storage.list_projects()

    @app.post("/api/projects", response_model=ProjectRecord, status_code=status.HTTP_201_CREATED)
    async def create_project(body: CreateProjectRequest) -> ProjectRecord:
        name = body.name.strip()
        if not name:
            raise ValueError("Project name must not be empty")
        root, created = _prepare_project_root(body.root, create=body.create_directory)
        project = ProjectRecord(id=uuid4().hex, name=name, root=str(root))
        try:
            storage.create_project(project)
        except Exception:
            if created:
                _remove_empty_directory(root)
            raise
        return project

    @app.get("/api/filesystem/directories")
    async def list_directories(path: str | None = None) -> dict[str, Any]:
        selected = resolve_workspace(
            path or storage.get_preference("last_workspace") or settings.workspace
        )
        return _directory_listing(selected)

    @app.get("/api/runs", response_model=list[RunView])
    async def list_runs(project_id: str | None = None, direct_only: bool = False) -> list[RunView]:
        if project_id and direct_only:
            raise ValueError("project_id and direct_only cannot be combined")
        records = storage.list_runs(project_id=project_id)
        if direct_only:
            records = [run for run in records if run.project_id is None]
        return [
            RunView.from_record(run, context_limit=settings.context_limit)
            for run in records
        ]

    @app.post("/api/runs", response_model=RunView, status_code=status.HTTP_201_CREATED)
    async def create_run(body: CreateRunRequest) -> RunView:
        project_id = body.project_id
        if project_id:
            project = storage.get_project(project_id)
            workspace = project.root
            storage.touch_project(project_id)
        else:
            workspace = (
                body.workspace
                or storage.get_preference("last_workspace")
                or str(settings.workspace)
            )
            workspace = str(resolve_workspace(workspace))
            storage.set_preference("last_workspace", workspace)
        run = await runtime.start_run(
            body.task,
            workspace,
            verifier_enabled=body.verifier_enabled,
            project_id=project_id,
        )
        return RunView.from_record(run, context_limit=settings.context_limit)

    @app.get("/api/runs/{run_id}", response_model=RunView)
    async def get_run(run_id: str) -> RunView:
        return RunView.from_record(
            storage.get_run(run_id), context_limit=settings.context_limit
        )

    @app.get("/api/runs/{run_id}/events", response_model=list[RunEvent])
    async def get_events(
        run_id: str,
        after_seq: Annotated[int, Query(ge=0)] = 0,
    ) -> list[RunEvent]:
        storage.get_run(run_id)
        return storage.get_events(run_id, after_seq=after_seq)

    @app.get("/api/runs/{run_id}/diff")
    async def get_diff(run_id: str) -> dict[str, str]:
        run = storage.get_run(run_id)
        return {"diff": Workspace(Path(run.workspace), storage).diff(run_id)}

    @app.get("/api/runs/{run_id}/proof-pack", response_model=ProofPack)
    async def get_proof_pack(run_id: str) -> ProofPack:
        return build_proof_pack(storage.get_run(run_id), storage)

    @app.get("/api/runs/{run_id}/proof-pack.md", response_class=PlainTextResponse)
    async def download_proof_pack(run_id: str) -> PlainTextResponse:
        pack = build_proof_pack(storage.get_run(run_id), storage)
        return PlainTextResponse(
            proof_pack_markdown(pack),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="traceforge-{run_id[:8]}-proof-pack.md"'
                )
            },
        )

    @app.post("/api/runs/{run_id}/answers", status_code=status.HTTP_202_ACCEPTED)
    async def answer_questions(
        run_id: str, body: ClarificationAnswersRequest
    ) -> dict[str, bool]:
        await runtime.manager_for_run(run_id).answer_clarification(run_id, body.answers)
        return {"accepted": True}

    @app.post("/api/runs/{run_id}/plan-decision", status_code=status.HTTP_202_ACCEPTED)
    async def decide_plan(run_id: str, body: PlanDecision) -> dict[str, bool]:
        await runtime.manager_for_run(run_id).decide_plan(run_id, body)
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
        await runtime.manager_for_run(run_id).decide_action(run_id, approved=body.approved)
        return {"accepted": True}

    @app.post("/api/runs/{run_id}/cancel", response_model=RunView)
    async def cancel_run(run_id: str) -> RunView:
        return RunView.from_record(
            await runtime.manager_for_run(run_id).cancel(run_id),
            context_limit=settings.context_limit,
        )

    @app.post("/api/runs/{run_id}/resume", response_model=RunView)
    async def resume_run(run_id: str) -> RunView:
        return RunView.from_record(
            await runtime.manager_for_run(run_id).resume(run_id),
            context_limit=settings.context_limit,
        )

    @app.post("/api/runs/{run_id}/rollback")
    async def rollback_run(run_id: str) -> dict[str, list[str]]:
        result = await runtime.manager_for_run(run_id).rollback(run_id)
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
        receive_task: asyncio.Task[MutableMapping[str, Any]] | None = None
        event_task: asyncio.Task[RunEvent] | None = None
        try:
            for event in storage.get_events(run_id, after_seq=after_seq):
                await websocket.send_json(event.model_dump(mode="json"))
                last_seq = event.seq
            receive_task = asyncio.create_task(websocket.receive())
            while True:
                assert receive_task is not None
                event_task = asyncio.create_task(queue.get())
                done, _pending = await asyncio.wait(
                    {event_task, receive_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if receive_task in done:
                    message = receive_task.result()
                    if message["type"] == "websocket.disconnect":
                        event_task.cancel()
                        await asyncio.gather(event_task, return_exceptions=True)
                        break
                    receive_task = asyncio.create_task(websocket.receive())
                if event_task not in done:
                    event_task.cancel()
                    await asyncio.gather(event_task, return_exceptions=True)
                    continue
                event = event_task.result()
                if event.seq <= last_seq:
                    continue
                await websocket.send_json(event.model_dump(mode="json"))
                last_seq = event.seq
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            pending_tasks = [
                task
                for task in (receive_task, event_task)
                if task is not None and not task.done()
            ]
            for task in pending_tasks:
                task.cancel()
            await asyncio.gather(*pending_tasks, return_exceptions=True)
            broker.unsubscribe(run_id, queue)

    _mount_frontend(app)
    return app


def _allowed_origin(origin: str) -> bool:
    return bool(re.fullmatch(r"https?://(?:127\.0\.0\.1|localhost)(?::\d+)?", origin))


def _prepare_project_root(raw: str, *, create: bool) -> tuple[Path, bool]:
    candidate = Path(raw).expanduser()
    if not create:
        return resolve_workspace(candidate), False
    if candidate.exists():
        raise ValueError(f"Project directory already exists: {candidate}")
    try:
        parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise ValueError(
            f"Project parent directory does not exist: {candidate.parent}"
        ) from exc
    if not parent.is_dir():
        raise ValueError(f"Project parent is not a directory: {parent}")
    try:
        candidate.mkdir()
    except OSError as exc:
        raise ValueError(f"Could not create project directory: {candidate}") from exc
    return resolve_workspace(candidate), True


def _remove_empty_directory(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def _directory_listing(selected: Path) -> dict[str, Any]:
    try:
        children = sorted(
            (
                {"name": child.name, "path": str(child)}
                for child in selected.iterdir()
                if child.is_dir() and not child.name.startswith(".")
            ),
            key=lambda item: item["name"].casefold(),
        )
    except OSError as exc:
        raise ValueError(f"Directory cannot be listed: {selected}") from exc
    parent = selected.parent if selected.parent != selected else None
    return {
        "current": str(selected),
        "parent": str(parent) if parent else None,
        "children": children,
    }


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

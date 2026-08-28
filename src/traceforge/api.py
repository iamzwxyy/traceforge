from __future__ import annotations

import asyncio
import platform
import re
import shutil
import subprocess
from collections.abc import AsyncIterator, MutableMapping
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
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
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from traceforge import __version__
from traceforge.agent import InvalidRunAction, PlanDecision, RunConflictError
from traceforge.config import Settings
from traceforge.context import ContextManager
from traceforge.events import EventBroker
from traceforge.model_context import resolve_model_context
from traceforge.model_reasoning import CATALOG_VERSION, resolve_reasoning_capability
from traceforge.models import (
    ApprovalMode,
    ApprovalRequest,
    ClarificationAnswer,
    ClarificationRequest,
    ConversationTurn,
    InteractionMode,
    PlanGate,
    ProjectRecord,
    ProofPack,
    ProviderConfig,
    ReasoningEffort,
    RunEvent,
    RunRecord,
    RunState,
    TaskPlan,
    VerificationReport,
)
from traceforge.proof import build_proof_pack, proof_pack_markdown
from traceforge.provider import ModelProvider
from traceforge.runtime import AgentRuntime, resolve_workspace
from traceforge.sandbox import sandbox_status
from traceforge.storage import Storage
from traceforge.tools import scrubbed_environment
from traceforge.workspace import Workspace


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=20_000)
    verifier_enabled: bool = True
    mode: InteractionMode = InteractionMode.AGENT
    approval_mode: ApprovalMode = ApprovalMode.AUTOMATIC
    reasoning_effort: ReasoningEffort = ReasoningEffort.AUTO
    project_id: str | None = None
    workspace: str | None = Field(default=None, max_length=4_096)
    create_direct_workspace: bool = False

    @model_validator(mode="after")
    def validate_target(self) -> CreateRunRequest:
        if self.workspace is not None and not self.workspace.strip():
            raise ValueError("Workspace must not be empty")
        selected_targets = sum(
            (bool(self.project_id), bool(self.workspace), self.create_direct_workspace)
        )
        if selected_targets > 1:
            raise ValueError(
                "Choose a project, an existing direct workspace, or a new direct workspace"
            )
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
    api_key: SecretStr | None = Field(default=None, max_length=16 * 1024)
    context_window: int | None = Field(default=None, ge=1, le=10_000_000)

    @model_validator(mode="after")
    def validate_credential_source(self) -> UpdateProviderRequest:
        if self.api_key is not None and self.credential_file:
            raise ValueError("Choose either an API key or a credential file, not both")
        return self


class ProviderConfigView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    base_url: str | None
    credential_source: str
    credential_file: str | None
    credential_env: str = "OPENAI_API_KEY"
    api_key_configured: bool
    connection_verified: bool
    verified_at: datetime | None
    context_window: int | None
    resolved_context_window: int
    context_window_source: Literal["configured", "catalog", "fallback"]
    supported_reasoning_efforts: list[ReasoningEffort]
    default_reasoning_effort: ReasoningEffort | None
    reasoning_effort_source: Literal[
        "openai_catalog", "deepseek_catalog", "provider_default"
    ]
    reasoning_effort_catalog_version: str
    updated_at: datetime


class ProviderProbeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    model: str
    latency_ms: int
    detail: str
    provider: ProviderConfigView


class ClarificationAnswersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: list[ClarificationAnswer] = Field(min_length=1, max_length=3)


class ActionDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool


class FollowUpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=20_000)
    mode: InteractionMode = InteractionMode.AGENT
    approval_mode: ApprovalMode = ApprovalMode.AUTOMATIC
    reasoning_effort: ReasoningEffort = ReasoningEffort.AUTO


class OpenWorkspaceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supported: bool
    opened: bool
    application: Literal["Finder", "file_manager"] | None = None


class RunView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    task: str
    workspace: str
    project_id: str | None
    state: RunState
    mode: InteractionMode
    approval_mode: ApprovalMode
    reasoning_effort: ReasoningEffort
    turns: list[ConversationTurn]
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
    def from_record(cls, run: RunRecord) -> RunView:
        public = run.model_dump(
            exclude={
                "messages",
                "plan_approved",
                "interrupted_from",
                "provider_reasoning_cleanup_pending",
            }
        )
        public["context_tokens"] = ContextManager(run.context_limit).estimated_tokens(
            run.messages
        )
        return cls.model_validate(public)


def create_app(
    settings: Settings,
    *,
    provider: ModelProvider | None = None,
    instance_id: str | None = None,
    instance_config_fingerprint: str | None = None,
) -> FastAPI:
    if (instance_id is None) != (instance_config_fingerprint is None):
        raise ValueError("Instance identity and configuration fingerprint must be paired")
    storage = Storage(settings.data_dir / "traceforge.db")
    storage.mark_all_active_runs_interrupted()
    broker = EventBroker(storage)
    runtime = AgentRuntime(settings, storage, broker, provider_override=provider)

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
    app.state.instance_id = instance_id
    app.state.instance_config_fingerprint = instance_config_fingerprint

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response: Response
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            fetch_site = request.headers.get("sec-fetch-site", "").lower()
            if (origin and not _allowed_origin(origin)) or fetch_site == "cross-site":
                response = JSONResponse(
                    status_code=403, content={"detail": "Cross-site request denied"}
                )
            else:
                response = await call_next(request)
        else:
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

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = [
            {
                "type": error.get("type", "value_error"),
                "loc": error.get("loc", ()),
                "msg": error.get("msg", "Invalid request"),
            }
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        payload = {"status": "ok", "version": __version__}
        if instance_id is not None:
            payload["instance_id"] = instance_id
        return payload

    @app.get("/api/status")
    async def get_status() -> dict[str, Any]:
        config = runtime.provider_config
        model_context = runtime.model_context
        last_workspace = str(_preferred_directory(storage, fallback=settings.workspace))
        return {
            "version": __version__,
            "workspace": str(settings.workspace),
            "last_workspace": last_workspace,
            "model": config.model,
            "base_url": config.base_url or "https://api.openai.com/v1",
            "api_key_configured": runtime.credential_configured(config),
            "connection_verified": runtime.connection_verified(),
            "suggested_task": settings.suggested_task,
            "mode": "demo" if settings.demo_mode else "standard",
            "sandbox": sandbox_status(settings.workspace).as_dict(),
            "limits": {
                "context": model_context.context_window,
                "context_source": model_context.source,
                "steps": settings.max_steps,
                "repair_cycles": settings.max_repair_cycles,
            },
        }

    def provider_view(config: ProviderConfig | None = None) -> ProviderConfigView:
        selected = config or runtime.provider_config
        model_context = resolve_model_context(
            selected.model,
            base_url=selected.base_url,
            configured_window=selected.context_window,
            fallback_window=settings.context_limit,
        )
        reasoning = resolve_reasoning_capability(
            selected.model, base_url=selected.base_url
        )
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
            connection_verified=runtime.connection_verified(),
            verified_at=storage.get_provider_verified_at(),
            context_window=selected.context_window,
            resolved_context_window=model_context.context_window,
            context_window_source=model_context.source,
            supported_reasoning_efforts=list(reasoning.supported_efforts),
            default_reasoning_effort=reasoning.default_effort,
            reasoning_effort_source=reasoning.source,
            reasoning_effort_catalog_version=CATALOG_VERSION,
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
                context_window=body.context_window,
            ),
            api_key=(body.api_key.get_secret_value() if body.api_key is not None else None),
        )
        return provider_view(saved)

    @app.post("/api/provider/test", response_model=ProviderProbeResponse)
    async def test_provider_connection(
        body: UpdateProviderRequest | None = None,
    ) -> ProviderProbeResponse:
        if body is None:
            result = await runtime.test_connection()
        else:
            result = await runtime.test_and_save_provider_config(
                ProviderConfig(
                    model=body.model,
                    base_url=body.base_url,
                    credential_file=body.credential_file,
                    context_window=body.context_window,
                ),
                api_key=(
                    body.api_key.get_secret_value()
                    if body.api_key is not None
                    else None
                ),
            )
        return ProviderProbeResponse(
            ok=bool(result["ok"]),
            model=str(result["model"]),
            latency_ms=int(result["latency_ms"]),
            detail=str(result["detail"]),
            provider=provider_view(),
        )

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
        storage.set_preference("last_workspace", str(root))
        return project

    @app.get("/api/filesystem/directories")
    async def list_directories(path: str | None = None) -> dict[str, Any]:
        selected = (
            resolve_workspace(path)
            if path
            else _preferred_directory(storage, fallback=settings.workspace)
        )
        return _directory_listing(selected)

    @app.post("/api/filesystem/choose-directory")
    async def choose_directory() -> dict[str, Any]:
        if settings.demo_mode or platform.system() != "Darwin":
            return {"supported": False, "path": None}
        initial = _preferred_directory(storage, fallback=settings.workspace)
        selected = await asyncio.to_thread(_choose_macos_directory, initial)
        return {"supported": True, "path": selected}

    @app.get("/api/runs", response_model=list[RunView])
    async def list_runs(project_id: str | None = None, direct_only: bool = False) -> list[RunView]:
        if project_id and direct_only:
            raise ValueError("project_id and direct_only cannot be combined")
        records = storage.list_runs(project_id=project_id)
        if direct_only:
            records = [run for run in records if run.project_id is None]
        return [RunView.from_record(run) for run in records]

    @app.post("/api/runs", response_model=RunView, status_code=status.HTTP_201_CREATED)
    async def create_run(body: CreateRunRequest) -> RunView:
        if settings.demo_mode:
            if body.task.strip() != (settings.suggested_task or "").strip():
                raise ValueError(
                    "The fixed demo only supports its prefilled task; use traceforge "
                    "for real tasks"
                )
            if body.project_id or body.workspace or body.create_direct_workspace:
                raise ValueError("The fixed demo only runs in its disposable demo workspace")
            if body.approval_mode is not ApprovalMode.AUTOMATIC:
                raise ValueError("The fixed demo only supports automatic approval mode")
            if body.reasoning_effort is not ReasoningEffort.AUTO:
                raise ValueError("The fixed demo only supports model-default reasoning")
        project_id = body.project_id
        created_workspace: Path | None = None
        if settings.demo_mode:
            workspace = str(settings.workspace)
        elif project_id:
            project = storage.get_project(project_id)
            workspace = project.root
            storage.touch_project(project_id)
        elif body.create_direct_workspace or body.workspace is None:
            created_workspace = _create_direct_workspace(settings.workspace)
            workspace = str(created_workspace)
        else:
            workspace = str(resolve_workspace(body.workspace))
            storage.set_preference("last_workspace", workspace)
        try:
            run = await runtime.start_run(
                body.task,
                workspace,
                verifier_enabled=body.verifier_enabled,
                project_id=project_id,
                mode=(InteractionMode.PLAN if settings.demo_mode else body.mode),
                approval_mode=(
                    ApprovalMode.AUTOMATIC if settings.demo_mode else body.approval_mode
                ),
                reasoning_effort=(
                    ReasoningEffort.AUTO if settings.demo_mode else body.reasoning_effort
                ),
            )
        except Exception:
            if created_workspace is not None:
                _remove_empty_directory(created_workspace)
            raise
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
        run = storage.get_run(run_id)
        return {"diff": Workspace(Path(run.workspace), storage).diff(run_id)}

    @app.post(
        "/api/runs/{run_id}/open-workspace",
        response_model=OpenWorkspaceResponse,
    )
    async def open_workspace(run_id: str) -> OpenWorkspaceResponse:
        run = storage.get_run(run_id)
        recorded_workspace = Path(run.workspace)
        workspace = resolve_workspace(recorded_workspace)
        if workspace != recorded_workspace:
            raise ValueError(
                "The recorded workspace path no longer points to its original directory"
            )
        return OpenWorkspaceResponse.model_validate(
            await asyncio.to_thread(_open_workspace_directory, workspace)
        )

    @app.get("/api/runs/{run_id}/proof-pack", response_model=ProofPack)
    async def get_proof_pack(run_id: str) -> ProofPack:
        run = storage.get_run(run_id)
        if run.state is RunState.ANSWERED:
            raise HTTPException(
                status_code=409,
                detail="The latest answer-only turn has no completion Proof Pack",
            )
        return build_proof_pack(run, storage)

    @app.get("/api/runs/{run_id}/plan.md", response_class=PlainTextResponse)
    async def download_plan(run_id: str) -> PlainTextResponse:
        run = storage.get_run(run_id)
        if run.plan is None:
            raise HTTPException(status_code=404, detail="No plan has been recorded")
        return PlainTextResponse(
            run.plan.markdown,
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="traceforge-{run_id[:8]}-plan.md"'
                )
            },
        )

    @app.get("/api/runs/{run_id}/proof-pack.md", response_class=PlainTextResponse)
    async def download_proof_pack(run_id: str) -> PlainTextResponse:
        run = storage.get_run(run_id)
        if run.state is RunState.ANSWERED:
            raise HTTPException(
                status_code=409,
                detail="The latest answer-only turn has no completion Proof Pack",
            )
        pack = build_proof_pack(run, storage)
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
    async def answer_questions(run_id: str, body: ClarificationAnswersRequest) -> dict[str, bool]:
        await runtime.manager_for_run(run_id).answer_clarification(run_id, body.answers)
        return {"accepted": True}

    @app.post("/api/runs/{run_id}/turns", response_model=RunView)
    async def follow_up(run_id: str, body: FollowUpRequest) -> RunView:
        if settings.demo_mode:
            raise ValueError(
                "The fixed demo is a single-turn guided tour; use traceforge "
                "for multi-turn tasks"
            )
        run = await runtime.follow_up(
            run_id,
            body.prompt,
            mode=body.mode,
            approval_mode=body.approval_mode,
            reasoning_effort=body.reasoning_effort,
        )
        return RunView.from_record(run)

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
        await runtime.manager_for_run(run_id).decide_action(
            run_id, approval_id, approved=body.approved
        )
        return {"accepted": True}

    @app.post("/api/runs/{run_id}/cancel", response_model=RunView)
    async def cancel_run(run_id: str) -> RunView:
        return RunView.from_record(
            await runtime.manager_for_run(run_id).cancel(run_id)
        )

    @app.post("/api/runs/{run_id}/resume", response_model=RunView)
    async def resume_run(run_id: str) -> RunView:
        return RunView.from_record(await runtime.resume_run(run_id))

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
                if event.seq > last_seq + 1:
                    # A bounded subscriber queue may evict old items for a slow client.
                    # SQLite is authoritative, so repair the gap before continuing live.
                    for persisted in storage.get_events(run_id, after_seq=last_seq):
                        await websocket.send_json(persisted.model_dump(mode="json"))
                        last_seq = persisted.seq
                else:
                    await websocket.send_json(event.model_dump(mode="json"))
                    last_seq = event.seq
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            pending_tasks = [
                task for task in (receive_task, event_task) if task is not None and not task.done()
            ]
            for task in pending_tasks:
                task.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.gather(*pending_tasks, return_exceptions=True)
            broker.unsubscribe(run_id, queue)

    _mount_frontend(app)
    return app


def _allowed_origin(origin: str) -> bool:
    return bool(
        re.fullmatch(
            r"https?://(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?", origin
        )
    )


def _preferred_directory(storage: Storage, *, fallback: Path) -> Path:
    preferred = storage.get_preference("last_workspace")
    for candidate in (preferred, Path.home(), fallback):
        if candidate is None:
            continue
        try:
            return resolve_workspace(candidate)
        except ValueError:
            continue
    return resolve_workspace(fallback)


def _prepare_project_root(raw: str, *, create: bool) -> tuple[Path, bool]:
    candidate = Path(raw).expanduser()
    if not create:
        return resolve_workspace(candidate), False
    if candidate.exists():
        raise ValueError(f"Project directory already exists: {candidate}")
    try:
        parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"Project parent directory does not exist: {candidate.parent}") from exc
    if not parent.is_dir():
        raise ValueError(f"Project parent is not a directory: {parent}")
    try:
        candidate.mkdir()
    except OSError as exc:
        raise ValueError(f"Could not create project directory: {candidate}") from exc
    return resolve_workspace(candidate), True


def _create_direct_workspace(default_root: Path) -> Path:
    root = resolve_workspace(default_root)
    for _attempt in range(10):
        candidate = root / f"traceforge-task-{uuid4().hex[:8]}"
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            continue
        except OSError as exc:
            raise ValueError(f"Could not create a direct-task directory under: {root}") from exc
        return resolve_workspace(candidate)
    raise ValueError(f"Could not allocate a unique direct-task directory under: {root}")


def _choose_macos_directory(initial_path: Path) -> str | None:
    script = (
        "on run argv\n"
        'set chosenFolder to choose folder with prompt "选择 TraceForge 项目文件夹" '
        "default location POSIX file (item 1 of argv)\n"
        "return POSIX path of chosenFolder\n"
        "end run"
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script, str(resolve_workspace(initial_path))],
            capture_output=True,
            check=False,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("The macOS folder picker could not be opened") from exc
    if result.returncode != 0:
        if "-128" in result.stderr or "User canceled" in result.stderr:
            return None
        raise ValueError("The macOS folder picker did not return a directory")
    selected = result.stdout.strip().rstrip("/") or "/"
    return str(resolve_workspace(selected))


def _open_workspace_directory(workspace: Path) -> dict[str, Any]:
    selected = resolve_workspace(workspace)
    system = platform.system()
    environment = scrubbed_environment()
    if system == "Darwin":
        executable = "/usr/bin/open"
        arguments = [executable, "-R", str(selected)]
        application: Literal["Finder", "file_manager"] = "Finder"
    elif system == "Linux":
        if not environment.get("DISPLAY") and not environment.get("WAYLAND_DISPLAY"):
            return {"supported": False, "opened": False, "application": None}
        discovered = shutil.which("xdg-open")
        executable_path = Path(discovered).resolve(strict=False) if discovered else None
        if executable_path is None or executable_path.is_relative_to(selected):
            return {"supported": False, "opened": False, "application": None}
        executable = str(executable_path)
        arguments = [executable, str(selected)]
        application = "file_manager"
    else:
        return {"supported": False, "opened": False, "application": None}
    try:
        result = subprocess.run(
            arguments,
            capture_output=True,
            check=False,
            env=environment,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("The local file manager could not be opened") from exc
    if result.returncode != 0:
        raise ValueError("The local file manager could not open this workspace")
    return {"supported": True, "opened": True, "application": application}


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

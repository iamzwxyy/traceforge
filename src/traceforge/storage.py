from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from traceforge.models import (
    ApprovalRequest,
    ClarificationRequest,
    EventType,
    PlanGate,
    ProjectRecord,
    ProviderConfig,
    RunEvent,
    RunRecord,
    RunState,
    TaskPlan,
    VerificationReport,
    utc_now,
)


@dataclass(frozen=True, slots=True)
class SnapshotRecord:
    run_id: str
    path: str
    existed: bool
    content: bytes | None
    mode: int | None
    original_hash: str | None
    last_agent_hash: str | None


class Storage:
    """Small synchronous SQLite repository protected for async web usage."""

    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    task TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    project_id TEXT,
                    state TEXT NOT NULL,
                    verifier_enabled INTEGER NOT NULL,
                    plan_json TEXT,
                    clarification_json TEXT,
                    pending_approval_json TEXT,
                    verification_json TEXT,
                    plan_gate_json TEXT,
                    messages_json TEXT NOT NULL DEFAULT '[]',
                    plan_approved INTEGER NOT NULL DEFAULT 0,
                    interrupted_from TEXT,
                    step_count INTEGER NOT NULL DEFAULT 0,
                    repair_cycles INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, seq),
                    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS snapshots (
                    run_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    existed INTEGER NOT NULL,
                    content BLOB,
                    mode INTEGER,
                    original_hash TEXT,
                    last_agent_hash TEXT,
                    PRIMARY KEY (run_id, path),
                    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    root TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_opened_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS provider_config (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    model TEXT NOT NULL,
                    base_url TEXT,
                    credential_file TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS preferences (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_runs_workspace_updated
                    ON runs(workspace, updated_at DESC);
                """
            )
            columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(runs)").fetchall()
            }
            migrations = {
                "plan_approved": "INTEGER NOT NULL DEFAULT 0",
                "interrupted_from": "TEXT",
                "project_id": "TEXT",
                "plan_gate_json": "TEXT",
            }
            for column, declaration in migrations.items():
                if column not in columns:
                    self._connection.execute(
                        f"ALTER TABLE runs ADD COLUMN {column} {declaration}"
                    )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_project_updated "
                "ON runs(project_id, updated_at DESC)"
            )

    def mark_active_runs_interrupted(self, workspace: Path) -> int:
        return self._mark_runs_interrupted("workspace = ?", (str(workspace),))

    def mark_all_active_runs_interrupted(self) -> int:
        return self._mark_runs_interrupted("1 = 1", ())

    def _mark_runs_interrupted(self, scope: str, parameters: tuple[str, ...]) -> int:
        active = tuple(
            state.value
            for state in RunState
            if not state.terminal and state is not RunState.INTERRUPTED
        )
        placeholders = ",".join("?" for _ in active)
        now = utc_now()
        with self._lock, self._connection:
            rows = self._connection.execute(
                f"""
                SELECT id, state FROM runs
                WHERE {scope} AND state IN ({placeholders})
                """,
                (*parameters, *active),
            ).fetchall()
            for row in rows:
                self._connection.execute(
                    """
                    UPDATE runs
                    SET interrupted_from = state, state = ?, error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        RunState.INTERRUPTED.value,
                        "TraceForge stopped before this run reached a terminal state.",
                        now.isoformat(),
                        row["id"],
                    ),
                )
                sequence = self._connection.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id = ?",
                    (row["id"],),
                ).fetchone()[0]
                self._connection.execute(
                    """
                    INSERT INTO events(run_id, seq, type, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        sequence,
                        EventType.STATE_CHANGED.value,
                        json.dumps(
                            {
                                "state": RunState.INTERRUPTED.value,
                                "previous": row["state"],
                                "cause": "process_restart",
                            },
                            ensure_ascii=False,
                        ),
                        now.isoformat(),
                    ),
                )
            return len(rows)

    def create_run(self, run: RunRecord) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO runs (
                    id, task, workspace, project_id, state, verifier_enabled, plan_json,
                    clarification_json, pending_approval_json, verification_json,
                    plan_gate_json, messages_json, plan_approved, interrupted_from,
                    step_count, repair_cycles, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._run_values(run),
            )

    def save_run(self, run: RunRecord) -> None:
        run.updated_at = utc_now()
        values = self._run_values(run)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE runs SET
                    task = ?, workspace = ?, project_id = ?, state = ?, verifier_enabled = ?,
                    plan_json = ?, clarification_json = ?, pending_approval_json = ?,
                    verification_json = ?, plan_gate_json = ?, messages_json = ?,
                    plan_approved = ?, interrupted_from = ?, step_count = ?,
                    repair_cycles = ?, error = ?, created_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (*values[1:], values[0]),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Run not found: {run.id}")

    def get_run(self, run_id: str) -> RunRecord:
        with self._lock:
            row = self._connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Run not found: {run_id}")
        return self._row_to_run(row)

    def list_runs(
        self,
        workspace: Path | None = None,
        *,
        project_id: str | None = None,
        limit: int = 100,
    ) -> list[RunRecord]:
        with self._lock:
            if workspace is not None:
                rows = self._connection.execute(
                    "SELECT * FROM runs WHERE workspace = ? ORDER BY updated_at DESC LIMIT ?",
                    (str(workspace), limit),
                ).fetchall()
            elif project_id is not None:
                rows = self._connection.execute(
                    "SELECT * FROM runs WHERE project_id = ? ORDER BY updated_at DESC LIMIT ?",
                    (project_id, limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM runs ORDER BY updated_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [self._row_to_run(row) for row in rows]

    def has_active_run(self, workspace: Path) -> bool:
        active = tuple(state.value for state in RunState if not state.terminal)
        placeholders = ",".join("?" for _ in active)
        with self._lock:
            row = self._connection.execute(
                f"SELECT 1 FROM runs WHERE workspace = ? AND state IN ({placeholders}) LIMIT 1",
                (str(workspace), *active),
            ).fetchone()
        return row is not None

    def has_any_active_run(self) -> bool:
        active = tuple(state.value for state in RunState if not state.terminal)
        placeholders = ",".join("?" for _ in active)
        with self._lock:
            row = self._connection.execute(
                f"SELECT 1 FROM runs WHERE state IN ({placeholders}) LIMIT 1", active
            ).fetchone()
        return row is not None

    def has_live_run(self) -> bool:
        """Return whether a run is executing or waiting on an in-process decision.

        Interrupted runs have no active task and may safely survive a provider-config change.
        """
        active = tuple(
            state.value
            for state in RunState
            if not state.terminal and state is not RunState.INTERRUPTED
        )
        placeholders = ",".join("?" for _ in active)
        with self._lock:
            row = self._connection.execute(
                f"SELECT 1 FROM runs WHERE state IN ({placeholders}) LIMIT 1", active
            ).fetchone()
        return row is not None

    def create_project(self, project: ProjectRecord) -> None:
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    """
                    INSERT INTO projects(id, name, root, created_at, updated_at, last_opened_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project.id,
                        project.name,
                        project.root,
                        project.created_at.isoformat(),
                        project.updated_at.isoformat(),
                        project.last_opened_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"A project already uses this directory: {project.root}") from exc

    def get_project(self, project_id: str) -> ProjectRecord:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Project not found: {project_id}")
        return self._row_to_project(row)

    def list_projects(self) -> list[ProjectRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM projects ORDER BY last_opened_at DESC, name COLLATE NOCASE"
            ).fetchall()
        return [self._row_to_project(row) for row in rows]

    def touch_project(self, project_id: str) -> None:
        now = utc_now().isoformat()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE projects SET updated_at = ?, last_opened_at = ? WHERE id = ?",
                (now, now, project_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Project not found: {project_id}")

    def get_provider_config(self, default: ProviderConfig) -> ProviderConfig:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM provider_config WHERE id = 1"
            ).fetchone()
        if row is None:
            return default
        return ProviderConfig(
            model=row["model"],
            base_url=row["base_url"],
            credential_file=row["credential_file"],
            updated_at=datetime.fromisoformat(row["updated_at"]).astimezone(UTC),
        )

    def save_provider_config(self, config: ProviderConfig) -> None:
        config.updated_at = utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO provider_config(id, model, base_url, credential_file, updated_at)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    model = excluded.model,
                    base_url = excluded.base_url,
                    credential_file = excluded.credential_file,
                    updated_at = excluded.updated_at
                """,
                (
                    config.model,
                    config.base_url,
                    config.credential_file,
                    config.updated_at.isoformat(),
                ),
            )

    def get_preference(self, key: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM preferences WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else str(row["value"])

    def set_preference(self, key: str, value: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO preferences(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def append_event(
        self, run_id: str, event_type: EventType, payload: dict[str, Any] | None = None
    ) -> RunEvent:
        created_at = utc_now()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            assert row is not None
            event = RunEvent(
                run_id=run_id,
                seq=int(row["seq"]),
                type=event_type,
                payload=payload or {},
                created_at=created_at,
            )
            self._connection.execute(
                """
                INSERT INTO events(run_id, seq, type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.run_id,
                    event.seq,
                    event.type.value,
                    json.dumps(event.payload, ensure_ascii=False),
                    event.created_at.isoformat(),
                ),
            )
        return event

    def get_events(self, run_id: str, *, after_seq: int = 0) -> list[RunEvent]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM events WHERE run_id = ? AND seq > ? ORDER BY seq",
                (run_id, after_seq),
            ).fetchall()
        return [
            RunEvent(
                run_id=row["run_id"],
                seq=row["seq"],
                type=EventType(row["type"]),
                payload=json.loads(row["payload_json"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def save_snapshot_if_absent(self, snapshot: SnapshotRecord) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO snapshots(
                    run_id, path, existed, content, mode, original_hash, last_agent_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.run_id,
                    snapshot.path,
                    int(snapshot.existed),
                    snapshot.content,
                    snapshot.mode,
                    snapshot.original_hash,
                    snapshot.last_agent_hash,
                ),
            )
            return cursor.rowcount == 1

    def update_snapshot_hash(self, run_id: str, path: str, last_agent_hash: str | None) -> None:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE snapshots SET last_agent_hash = ? WHERE run_id = ? AND path = ?",
                (last_agent_hash, run_id, path),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Snapshot not found: {run_id}:{path}")

    def list_snapshots(self, run_id: str) -> list[SnapshotRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM snapshots WHERE run_id = ? ORDER BY path", (run_id,)
            ).fetchall()
        return [
            SnapshotRecord(
                run_id=row["run_id"],
                path=row["path"],
                existed=bool(row["existed"]),
                content=row["content"],
                mode=row["mode"],
                original_hash=row["original_hash"],
                last_agent_hash=row["last_agent_hash"],
            )
            for row in rows
        ]

    @staticmethod
    def _run_values(run: RunRecord) -> tuple[Any, ...]:
        return (
            run.id,
            run.task,
            run.workspace,
            run.project_id,
            run.state.value,
            int(run.verifier_enabled),
            _dump_model(run.plan),
            _dump_model(run.clarification),
            _dump_model(run.pending_approval),
            _dump_model(run.verification),
            _dump_model(run.plan_gate),
            json.dumps(run.messages, ensure_ascii=False),
            int(run.plan_approved),
            run.interrupted_from.value if run.interrupted_from else None,
            run.step_count,
            run.repair_cycles,
            run.error,
            run.created_at.isoformat(),
            run.updated_at.isoformat(),
        )

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=row["id"],
            task=row["task"],
            workspace=row["workspace"],
            project_id=row["project_id"],
            state=RunState(row["state"]),
            verifier_enabled=bool(row["verifier_enabled"]),
            plan=_load_model(TaskPlan, row["plan_json"]),
            clarification=_load_model(ClarificationRequest, row["clarification_json"]),
            pending_approval=_load_model(ApprovalRequest, row["pending_approval_json"]),
            verification=_load_model(VerificationReport, row["verification_json"]),
            plan_gate=_load_model(PlanGate, row["plan_gate_json"]),
            messages=json.loads(row["messages_json"]),
            plan_approved=bool(row["plan_approved"]),
            interrupted_from=(
                RunState(row["interrupted_from"]) if row["interrupted_from"] else None
            ),
            step_count=row["step_count"],
            repair_cycles=row["repair_cycles"],
            error=row["error"],
            created_at=datetime.fromisoformat(row["created_at"]).astimezone(UTC),
            updated_at=datetime.fromisoformat(row["updated_at"]).astimezone(UTC),
        )

    @staticmethod
    def _row_to_project(row: sqlite3.Row) -> ProjectRecord:
        return ProjectRecord(
            id=row["id"],
            name=row["name"],
            root=row["root"],
            created_at=datetime.fromisoformat(row["created_at"]).astimezone(UTC),
            updated_at=datetime.fromisoformat(row["updated_at"]).astimezone(UTC),
            last_opened_at=datetime.fromisoformat(row["last_opened_at"]).astimezone(UTC),
        )


def _dump_model(value: BaseModel | None) -> str | None:
    if value is None:
        return None
    return value.model_dump_json()


def _load_model[ModelT: BaseModel](model: type[ModelT], raw: str | None) -> ModelT | None:
    if raw is None:
        return None
    return model.model_validate_json(raw)

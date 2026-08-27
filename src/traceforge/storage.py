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
                    state TEXT NOT NULL,
                    verifier_enabled INTEGER NOT NULL,
                    plan_json TEXT,
                    clarification_json TEXT,
                    pending_approval_json TEXT,
                    verification_json TEXT,
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
            }
            for column, declaration in migrations.items():
                if column not in columns:
                    self._connection.execute(
                        f"ALTER TABLE runs ADD COLUMN {column} {declaration}"
                    )

    def mark_active_runs_interrupted(self, workspace: Path) -> int:
        active = tuple(
            state.value
            for state in RunState
            if not state.terminal and state is not RunState.INTERRUPTED
        )
        placeholders = ",".join("?" for _ in active)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                f"""
                UPDATE runs
                SET interrupted_from = state, state = ?, error = ?, updated_at = ?
                WHERE workspace = ? AND state IN ({placeholders})
                """,
                (
                    RunState.INTERRUPTED.value,
                    "TraceForge stopped before this run reached a terminal state.",
                    utc_now().isoformat(),
                    str(workspace),
                    *active,
                ),
            )
            return cursor.rowcount

    def create_run(self, run: RunRecord) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO runs (
                    id, task, workspace, state, verifier_enabled, plan_json,
                    clarification_json, pending_approval_json, verification_json,
                    messages_json, plan_approved, interrupted_from, step_count,
                    repair_cycles, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    task = ?, workspace = ?, state = ?, verifier_enabled = ?,
                    plan_json = ?, clarification_json = ?, pending_approval_json = ?,
                    verification_json = ?, messages_json = ?, plan_approved = ?,
                    interrupted_from = ?, step_count = ?, repair_cycles = ?,
                    error = ?, created_at = ?, updated_at = ?
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

    def list_runs(self, workspace: Path, *, limit: int = 100) -> list[RunRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM runs WHERE workspace = ? ORDER BY updated_at DESC LIMIT ?",
                (str(workspace), limit),
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
            run.state.value,
            int(run.verifier_enabled),
            _dump_model(run.plan),
            _dump_model(run.clarification),
            _dump_model(run.pending_approval),
            _dump_model(run.verification),
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
            state=RunState(row["state"]),
            verifier_enabled=bool(row["verifier_enabled"]),
            plan=_load_model(TaskPlan, row["plan_json"]),
            clarification=_load_model(ClarificationRequest, row["clarification_json"]),
            pending_approval=_load_model(ApprovalRequest, row["pending_approval_json"]),
            verification=_load_model(VerificationReport, row["verification_json"]),
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


def _dump_model(value: BaseModel | None) -> str | None:
    if value is None:
        return None
    return value.model_dump_json()


def _load_model[ModelT: BaseModel](model: type[ModelT], raw: str | None) -> ModelT | None:
    if raw is None:
        return None
    return model.model_validate_json(raw)

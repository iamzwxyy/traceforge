from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from traceforge.credentials import (
    CREDENTIAL_CONFLICT_CAUSE,
    CREDENTIAL_CONFLICT_DISCARDED_SUBJECT,
    CREDENTIAL_CONFLICT_SUMMARY,
    CREDENTIAL_CONFLICT_TASK,
)
from traceforge.models import (
    ApprovalMode,
    ApprovalRequest,
    ClarificationRequest,
    ConversationTurn,
    DecisionKind,
    DecisionRequest,
    DecisionStatus,
    EventType,
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
    WorkspaceInstructionSnapshot,
    utc_now,
)
from traceforge.streaming import (
    boundary_safe_json_dumps,
    contains_secret_representation,
    redact_json_value,
    redact_text,
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


class SecureCheckpointError(RuntimeError):
    """A WAL truncation needed for provider-private cleanup could not be confirmed."""


class DecisionConflictError(RuntimeError):
    """A durable user-decision request was stale, conflicting, or invalid."""


class CredentialPersistenceError(RuntimeError):
    """Credential-like data reached a durable or public serialization boundary."""


class Storage:
    """Small synchronous SQLite repository protected for async web usage."""

    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            database_path.parent.chmod(0o700)
        self._database_path = database_path
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._guarded_credentials: set[str] = set()
        try:
            cleanup_pending = self._initialize()
            if cleanup_pending:
                self.secure_checkpoint()
                with self._lock, self._connection:
                    self._connection.execute(
                        "UPDATE runs SET provider_reasoning_cleanup_pending = 0 "
                        "WHERE provider_reasoning_cleanup_pending = 1"
                    )
            self._secure_database_files()
        except BaseException:
            self._secure_database_files()
            self._connection.close()
            raise

    def register_credential_guard(self, api_key: str) -> None:
        """Keep a credential in memory so later writes and public projections fail closed."""

        if not api_key:
            return
        if "\n" in api_key or "\r" in api_key:
            raise ValueError("Provider credentials must contain exactly one line")
        with self._lock:
            self._guarded_credentials.add(api_key)

    def redact_public_value(self, value: Any) -> Any:
        """Return a deep redacted copy suitable for a REST or WebSocket response."""

        safe = value
        try:
            safe = redact_json_value(safe, api_key="")
            with self._lock:
                credentials = tuple(self._guarded_credentials)
            for api_key in credentials:
                safe = redact_json_value(safe, api_key=api_key)
        except ValueError as exc:
            raise CredentialPersistenceError(
                "Public data could not be redacted without changing its structure"
            ) from exc
        self._assert_safe_value(safe)
        return safe

    def redact_public_text(self, value: str) -> str:
        safe = redact_text(value, api_key="")
        with self._lock:
            credentials = tuple(self._guarded_credentials)
        for api_key in credentials:
            safe = redact_text(safe, api_key=api_key)
        self._assert_safe_text(safe)
        return safe

    def render_public_json(self, value: Any) -> bytes:
        safe = self.redact_public_value(value)
        rendered = boundary_safe_json_dumps(safe)
        self._assert_safe_text(rendered)
        return rendered.encode("utf-8")

    def _assert_safe_text(self, value: str) -> None:
        if contains_secret_representation(value, api_key=""):
            raise CredentialPersistenceError(
                "Credential-like data cannot cross this serialization boundary"
            )
        with self._lock:
            credentials = tuple(self._guarded_credentials)
        if any(
            contains_secret_representation(value, api_key=api_key)
            for api_key in credentials
        ):
            raise CredentialPersistenceError(
                "Credential-like data cannot cross this serialization boundary"
            )

    def _assert_safe_value(self, value: Any) -> None:
        if isinstance(value, BaseModel):
            self._assert_safe_value(value.model_dump(mode="json"))
            return
        if isinstance(value, str):
            self._assert_safe_text(value)
            return
        if isinstance(value, bytes):
            self._assert_safe_text(value.decode("utf-8", errors="ignore"))
            with self._lock:
                credentials = tuple(self._guarded_credentials)
            if any(api_key.encode("utf-8") in value for api_key in credentials):
                raise CredentialPersistenceError(
                    "Credential-like data cannot cross this serialization boundary"
                )
            return
        if isinstance(value, dict):
            for key, item in value.items():
                self._assert_safe_value(key)
                self._assert_safe_value(item)
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                self._assert_safe_value(item)

    def _json_dumps(self, value: Any, *, sort_keys: bool = False) -> str:
        self._assert_safe_value(value)
        rendered = boundary_safe_json_dumps(value, sort_keys=sort_keys)
        self._assert_safe_text(rendered)
        return rendered

    def close(self) -> None:
        with self._lock:
            self._secure_database_files()
            self._connection.close()

    def secure_checkpoint(
        self, *, attempts: int = 10, retry_delay: float = 0.01
    ) -> None:
        """Flush and truncate WAL, failing if private-state cleanup cannot be confirmed."""

        if attempts < 1:
            raise ValueError("Checkpoint attempts must be at least one")
        if retry_delay < 0:
            raise ValueError("Checkpoint retry delay cannot be negative")
        with self._lock:
            timeout_row = self._connection.execute("PRAGMA busy_timeout").fetchone()
            previous_timeout = int(timeout_row[0]) if timeout_row is not None else 0
            self._connection.execute("PRAGMA busy_timeout = 0")
            try:
                for attempt in range(attempts):
                    row = self._connection.execute(
                        "PRAGMA wal_checkpoint(TRUNCATE)"
                    ).fetchone()
                    if row is not None and int(row[0]) == 0:
                        self._secure_database_files()
                        return
                    if attempt + 1 < attempts:
                        time.sleep(retry_delay)
                self._secure_database_files()
                raise SecureCheckpointError(
                    "SQLite WAL remained busy; private-state cleanup could not be confirmed"
                )
            finally:
                self._connection.execute(
                    f"PRAGMA busy_timeout = {previous_timeout}"
                )

    def _secure_database_files(self) -> None:
        if os.name != "posix":
            return
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self._database_path}{suffix}")
            try:
                metadata = candidate.lstat()
            except OSError:
                continue
            if stat.S_ISREG(metadata.st_mode):
                candidate.chmod(0o600)

    def _initialize(self) -> bool:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA foreign_keys = ON;
                PRAGMA secure_delete = ON;

                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    task TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    project_id TEXT,
                    state TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'agent',
                    approval_mode TEXT NOT NULL DEFAULT 'automatic',
                    reasoning_effort TEXT NOT NULL DEFAULT 'auto',
                    turns_json TEXT NOT NULL DEFAULT '[]',
                    verifier_enabled INTEGER NOT NULL,
                    plan_json TEXT,
                    clarification_json TEXT,
                    pending_approval_json TEXT,
                    verification_json TEXT,
                    plan_gate_json TEXT,
                    messages_json TEXT NOT NULL DEFAULT '[]',
                    provider_reasoning_cleanup_pending INTEGER NOT NULL DEFAULT 0,
                    plan_approved INTEGER NOT NULL DEFAULT 0,
                    interrupted_from TEXT,
                    step_count INTEGER NOT NULL DEFAULT 0,
                    repair_cycles INTEGER NOT NULL DEFAULT 0,
                    context_limit INTEGER NOT NULL DEFAULT 64000,
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

                CREATE TABLE IF NOT EXISTS proof_packs (
                    run_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL CHECK(turn_index >= 1),
                    proof_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, turn_index),
                    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS workspace_instruction_snapshots (
                    run_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL CHECK(turn_index >= 1),
                    schema_version TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, turn_index),
                    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS proof_backfill_candidates (
                    run_id TEXT PRIMARY KEY,
                    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS decision_requests (
                    run_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    turn_index INTEGER NOT NULL CHECK(turn_index >= 1),
                    subject_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT,
                    payload_sha256 TEXT,
                    created_at TEXT NOT NULL,
                    accepted_at TEXT,
                    consumed_at TEXT,
                    execution_started_at TEXT,
                    PRIMARY KEY (run_id, request_id),
                    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS run_lineage (
                    child_run_id TEXT PRIMARY KEY,
                    parent_run_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (child_run_id) REFERENCES runs(id) ON DELETE CASCADE,
                    FOREIGN KEY (parent_run_id) REFERENCES runs(id) ON DELETE RESTRICT
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
                    context_window INTEGER,
                    updated_at TEXT NOT NULL,
                    verified_at TEXT
                );

                CREATE TABLE IF NOT EXISTS preferences (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS schema_migrations (
                    name TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_runs_workspace_updated
                    ON runs(workspace, updated_at DESC);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_decision_requests_active
                    ON decision_requests(run_id)
                    WHERE status IN ('pending', 'accepted');

                CREATE UNIQUE INDEX IF NOT EXISTS idx_run_lineage_parent
                    ON run_lineage(parent_run_id);
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
                "mode": "TEXT NOT NULL DEFAULT 'agent'",
                "approval_mode": "TEXT NOT NULL DEFAULT 'automatic'",
                "reasoning_effort": "TEXT NOT NULL DEFAULT 'auto'",
                "turns_json": "TEXT NOT NULL DEFAULT '[]'",
                "context_limit": "INTEGER NOT NULL DEFAULT 64000",
                "provider_reasoning_cleanup_pending": "INTEGER NOT NULL DEFAULT 0",
            }
            for column, declaration in migrations.items():
                if column not in columns:
                    self._connection.execute(
                        f"ALTER TABLE runs ADD COLUMN {column} {declaration}"
                    )
            provider_columns = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(provider_config)"
                ).fetchall()
            }
            if "context_window" not in provider_columns:
                self._connection.execute(
                    "ALTER TABLE provider_config ADD COLUMN context_window INTEGER"
                )
            if "verified_at" not in provider_columns:
                self._connection.execute(
                    "ALTER TABLE provider_config ADD COLUMN verified_at TEXT"
                )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_project_updated "
                "ON runs(project_id, updated_at DESC)"
            )
            proof_migration = self._connection.execute(
                "SELECT 1 FROM schema_migrations WHERE name = ?",
                ("proof-packs-v2",),
            ).fetchone()
            if proof_migration is None:
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO proof_backfill_candidates(run_id)
                    SELECT runs.id FROM runs
                    WHERE runs.state = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM proof_packs
                          WHERE proof_packs.run_id = runs.id
                      )
                    """,
                    (RunState.SUCCEEDED.value,),
                )
                self._connection.execute(
                    "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                    ("proof-packs-v2", utc_now().isoformat()),
                )
            credential_migration = self._connection.execute(
                "SELECT 1 FROM schema_migrations WHERE name = ?",
                ("credential-boundary-v1",),
            ).fetchone()
            if credential_migration is None:
                self._invalidate_pre_boundary_action_decisions()
                self._connection.execute(
                    "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                    ("credential-boundary-v1", utc_now().isoformat()),
                )
            instruction_migration = self._connection.execute(
                "SELECT 1 FROM schema_migrations WHERE name = ?",
                ("workspace-instructions-v1",),
            ).fetchone()
            if instruction_migration is None:
                self._invalidate_pre_instruction_decisions()
                self._connection.execute(
                    "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                    ("workspace-instructions-v1", utc_now().isoformat()),
                )
            self._scrub_terminal_reasoning_rows()
            row = self._connection.execute(
                "SELECT 1 FROM runs WHERE provider_reasoning_cleanup_pending = 1 LIMIT 1"
            ).fetchone()
            return row is not None

    def _invalidate_pre_boundary_action_decisions(self) -> None:
        """Never execute an action approval persisted before provider-ingress hardening."""

        rows = self._connection.execute(
            """
            SELECT DISTINCT run_id FROM decision_requests
            WHERE kind = ? AND status IN (?, ?)
            """,
            (
                DecisionKind.ACTION.value,
                DecisionStatus.PENDING.value,
                DecisionStatus.ACCEPTED.value,
            ),
        ).fetchall()
        if not rows:
            return
        now = utc_now()
        for row in rows:
            run_id = str(row["run_id"])
            self._connection.execute(
                """
                UPDATE decision_requests
                SET status = ?, consumed_at = ?
                WHERE run_id = ? AND kind = ? AND status IN (?, ?)
                """,
                (
                    DecisionStatus.ABANDONED.value,
                    now.isoformat(),
                    run_id,
                    DecisionKind.ACTION.value,
                    DecisionStatus.PENDING.value,
                    DecisionStatus.ACCEPTED.value,
                ),
            )
            self._connection.execute(
                """
                UPDATE runs
                SET pending_approval_json = NULL, messages_json = ?
                WHERE id = ?
                """,
                (boundary_safe_json_dumps([]), run_id),
            )
            self._connection.execute(
                """
                INSERT INTO events(run_id, seq, type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    self._next_event_seq_locked(run_id),
                    EventType.DECISION_ABANDONED.value,
                    boundary_safe_json_dumps(
                        {
                            "kind": DecisionKind.ACTION.value,
                            "cause": "credential_boundary_upgrade",
                        }
                    ),
                    now.isoformat(),
                ),
            )

    def _invalidate_pre_instruction_decisions(self) -> None:
        """Abandon every interactive receipt whose turn predates rule snapshots."""

        rows = self._connection.execute(
            """
            SELECT decisions.run_id, decisions.request_id, decisions.kind
            FROM decision_requests AS decisions
            WHERE decisions.status IN (?, ?)
              AND NOT EXISTS (
                  SELECT 1 FROM workspace_instruction_snapshots AS snapshots
                  WHERE snapshots.run_id = decisions.run_id
                    AND snapshots.turn_index = decisions.turn_index
              )
            """,
            (
                DecisionStatus.PENDING.value,
                DecisionStatus.ACCEPTED.value,
            ),
        ).fetchall()
        if not rows:
            return
        now = utc_now()
        for row in rows:
            run_id = str(row["run_id"])
            request_id = str(row["request_id"])
            kind = DecisionKind(str(row["kind"]))
            self._connection.execute(
                """
                UPDATE decision_requests
                SET status = ?, consumed_at = ?
                WHERE run_id = ? AND request_id = ? AND status IN (?, ?)
                """,
                (
                    DecisionStatus.ABANDONED.value,
                    now.isoformat(),
                    run_id,
                    request_id,
                    DecisionStatus.PENDING.value,
                    DecisionStatus.ACCEPTED.value,
                ),
            )
            self._connection.execute(
                """
                UPDATE runs
                SET clarification_json = NULL, pending_approval_json = NULL, messages_json = ?
                WHERE id = ?
                """,
                (boundary_safe_json_dumps([]), run_id),
            )
            self._connection.execute(
                """
                INSERT INTO events(run_id, seq, type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    self._next_event_seq_locked(run_id),
                    EventType.DECISION_ABANDONED.value,
                    boundary_safe_json_dumps(
                        {
                            "kind": kind.value,
                            "cause": "workspace_instruction_upgrade",
                        }
                    ),
                    now.isoformat(),
                ),
            )

    def _scrub_terminal_reasoning_rows(self) -> None:
        """Remove private provider replay fields left by older terminal-run ordering."""

        terminal = tuple(state.value for state in RunState if state.terminal)
        placeholders = ",".join("?" for _ in terminal)
        rows = self._connection.execute(
            f"SELECT id, messages_json FROM runs WHERE state IN ({placeholders})",
            terminal,
        ).fetchall()
        for row in rows:
            messages = json.loads(row["messages_json"])
            if not isinstance(messages, list):
                continue
            changed = False
            for message in messages:
                if isinstance(message, dict) and "reasoning_content" in message:
                    message.pop("reasoning_content")
                    changed = True
            if changed:
                self._connection.execute(
                    "UPDATE runs SET messages_json = ?, "
                    "provider_reasoning_cleanup_pending = 1 WHERE id = ?",
                    (boundary_safe_json_dumps(messages), row["id"]),
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
                self._abort_open_assistant_output_streams_locked(
                    str(row["id"]),
                    status="interrupted",
                    reason="process_restart",
                    created_at=now,
                )
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
                sequence = self._next_event_seq_locked(str(row["id"]))
                self._connection.execute(
                    """
                    INSERT INTO events(run_id, seq, type, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        sequence,
                        EventType.STATE_CHANGED.value,
                        boundary_safe_json_dumps(
                            {
                                "state": RunState.INTERRUPTED.value,
                                "previous": row["state"],
                                "cause": "process_restart",
                            }
                        ),
                        now.isoformat(),
                    ),
                )
            return len(rows)

    def create_run(
        self,
        run: RunRecord,
        *,
        parent_run_id: str | None = None,
        instruction_snapshot: WorkspaceInstructionSnapshot | None = None,
        initial_events: list[tuple[EventType, dict[str, Any]]] | None = None,
    ) -> list[RunEvent]:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO runs (
                    id, task, workspace, project_id, state, mode, approval_mode,
                    reasoning_effort, turns_json,
                    verifier_enabled, plan_json,
                    clarification_json, pending_approval_json, verification_json,
                    plan_gate_json, messages_json, provider_reasoning_cleanup_pending,
                    plan_approved, interrupted_from,
                    step_count, repair_cycles, context_limit, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._run_values(run),
            )
            if parent_run_id is not None:
                parent = self._connection.execute(
                    "SELECT state FROM runs WHERE id = ?", (parent_run_id,)
                ).fetchone()
                if parent is None:
                    raise KeyError(f"Parent run not found: {parent_run_id}")
                if parent["state"] != RunState.ROLLED_BACK.value:
                    raise ValueError("Only a rolled-back run can start a successor")
                self._connection.execute(
                    """
                    INSERT INTO run_lineage(child_run_id, parent_run_id, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (run.id, parent_run_id, run.created_at.isoformat()),
                )
            if instruction_snapshot is not None:
                turn_index = run.turns[-1].index if run.turns else 1
                self._insert_workspace_instruction_snapshot_locked(
                    run.id,
                    turn_index,
                    instruction_snapshot,
                )
            events: list[RunEvent] = []
            first_seq = self._next_event_seq_locked(run.id)
            for offset, (event_type, payload) in enumerate(initial_events or []):
                event = RunEvent(
                    run_id=run.id,
                    seq=first_seq + offset,
                    type=event_type,
                    payload=payload,
                    created_at=run.created_at,
                )
                self._insert_event_locked(event)
                events.append(event)
            return events

    def insert_workspace_instruction_snapshot(
        self,
        run_id: str,
        turn_index: int,
        snapshot: WorkspaceInstructionSnapshot,
    ) -> None:
        """Insert one immutable turn snapshot; existing rows are never overwritten."""

        with self._lock, self._connection:
            self._insert_workspace_instruction_snapshot_locked(
                run_id,
                turn_index,
                snapshot,
            )

    def get_workspace_instruction_snapshot(
        self,
        run_id: str,
        turn_index: int,
    ) -> WorkspaceInstructionSnapshot:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT snapshot_json FROM workspace_instruction_snapshots
                WHERE run_id = ? AND turn_index = ?
                """,
                (run_id, turn_index),
            ).fetchone()
        if row is None:
            raise KeyError(
                f"Workspace instruction snapshot not found: {run_id} turn {turn_index}"
            )
        return WorkspaceInstructionSnapshot.model_validate_json(row["snapshot_json"])

    def try_get_workspace_instruction_snapshot(
        self,
        run_id: str,
        turn_index: int,
    ) -> WorkspaceInstructionSnapshot | None:
        try:
            return self.get_workspace_instruction_snapshot(run_id, turn_index)
        except KeyError:
            return None

    def begin_turn(
        self,
        run: RunRecord,
        *,
        previous_state: RunState,
        instruction_snapshot: WorkspaceInstructionSnapshot,
        events: list[tuple[EventType, dict[str, Any]]],
    ) -> list[RunEvent]:
        """Atomically persist a follow-up turn, its rule snapshot, and visible events."""

        if not run.turns:
            raise ValueError("A follow-up run must contain a conversation turn")
        now = utc_now()
        with self._lock, self._connection:
            current = self._connection.execute(
                "SELECT state FROM runs WHERE id = ?",
                (run.id,),
            ).fetchone()
            if current is None:
                raise KeyError(f"Run not found: {run.id}")
            if current["state"] != previous_state.value:
                raise DecisionConflictError("Run state changed before the follow-up began")
            self._insert_workspace_instruction_snapshot_locked(
                run.id,
                run.turns[-1].index,
                instruction_snapshot,
            )
            run.updated_at = now
            self._update_run_locked(run, expected_state=previous_state)
            first_seq = self._next_event_seq_locked(run.id)
            persisted: list[RunEvent] = []
            for offset, (event_type, payload) in enumerate(events):
                event = RunEvent(
                    run_id=run.id,
                    seq=first_seq + offset,
                    type=event_type,
                    payload=payload,
                    created_at=now,
                )
                self._insert_event_locked(event)
                persisted.append(event)
            return persisted

    def get_parent_run_id(self, run_id: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT parent_run_id FROM run_lineage WHERE child_run_id = ?",
                (run_id,),
            ).fetchone()
        return None if row is None else str(row["parent_run_id"])

    def get_successor_run_id(self, run_id: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT child_run_id FROM run_lineage WHERE parent_run_id = ?",
                (run_id,),
            ).fetchone()
        return None if row is None else str(row["child_run_id"])

    def save_run(self, run: RunRecord) -> None:
        run.updated_at = utc_now()
        with self._lock, self._connection:
            self._update_run_locked(run)

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
        self._assert_safe_value(project)
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
            context_window=row["context_window"],
            updated_at=datetime.fromisoformat(row["updated_at"]).astimezone(UTC),
        )

    def get_provider_verified_at(self) -> datetime | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT verified_at FROM provider_config WHERE id = 1"
            ).fetchone()
        if row is None or row["verified_at"] is None:
            return None
        return datetime.fromisoformat(row["verified_at"]).astimezone(UTC)

    def save_provider_config(
        self, config: ProviderConfig, *, verified_at: datetime | None = None
    ) -> None:
        config.updated_at = utc_now()
        self._assert_safe_value(config)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO provider_config(
                    id, model, base_url, credential_file, context_window,
                    updated_at, verified_at
                )
                VALUES (1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    model = excluded.model,
                    base_url = excluded.base_url,
                    credential_file = excluded.credential_file,
                    context_window = excluded.context_window,
                    updated_at = excluded.updated_at,
                    verified_at = excluded.verified_at
                """,
                (
                    config.model,
                    config.base_url,
                    config.credential_file,
                    config.context_window,
                    config.updated_at.isoformat(),
                    verified_at.isoformat() if verified_at is not None else None,
                ),
            )

    def get_preference(self, key: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM preferences WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else str(row["value"])

    def set_preference(self, key: str, value: str) -> None:
        self._assert_safe_value({key: value})
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO preferences(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def get_decision(self, run_id: str, request_id: str) -> DecisionRequest:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM decision_requests WHERE run_id = ? AND request_id = ?",
                (run_id, request_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"Decision request not found: {request_id}")
        return self._row_to_decision(row)

    def get_active_decision(self, run_id: str) -> DecisionRequest | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM decision_requests
                WHERE run_id = ? AND status IN (?, ?)
                ORDER BY created_at DESC LIMIT 1
                """,
                (
                    run_id,
                    DecisionStatus.PENDING.value,
                    DecisionStatus.ACCEPTED.value,
                ),
            ).fetchone()
        return None if row is None else self._row_to_decision(row)

    def open_decision(
        self,
        run: RunRecord,
        *,
        previous_state: RunState,
        request_id: str,
        kind: DecisionKind,
        turn_index: int,
        subject: dict[str, Any],
        requested_event_type: EventType,
        requested_payload: dict[str, Any],
    ) -> tuple[DecisionRequest, list[RunEvent]]:
        """Atomically expose a waiting run, its durable inbox row, and request event."""

        if run.state is previous_state:
            raise ValueError("A decision window must enter a distinct waiting state")
        now = utc_now()
        subject_sha256 = decision_payload_sha256(subject)
        record = DecisionRequest(
            run_id=run.id,
            request_id=request_id,
            kind=kind,
            turn_index=turn_index,
            subject_sha256=subject_sha256,
            status=DecisionStatus.PENDING,
            created_at=now,
        )
        with self._lock, self._connection:
            current = self._connection.execute(
                "SELECT state FROM runs WHERE id = ?", (run.id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"Run not found: {run.id}")
            if current["state"] != previous_state.value:
                raise DecisionConflictError("Run state changed before the decision window opened")
            active = self._connection.execute(
                """
                SELECT request_id FROM decision_requests
                WHERE run_id = ? AND status IN (?, ?)
                """,
                (
                    run.id,
                    DecisionStatus.PENDING.value,
                    DecisionStatus.ACCEPTED.value,
                ),
            ).fetchone()
            if active is not None:
                raise DecisionConflictError("Another decision request is already active")
            run.updated_at = now
            self._update_run_locked(run, expected_state=previous_state)
            self._insert_decision_locked(record)
            first_seq = self._next_event_seq_locked(run.id)
            events = [
                RunEvent(
                    run_id=run.id,
                    seq=first_seq,
                    type=EventType.STATE_CHANGED,
                    payload={
                        "state": run.state.value,
                        "previous": previous_state.value,
                    },
                    created_at=now,
                ),
                RunEvent(
                    run_id=run.id,
                    seq=first_seq + 1,
                    type=requested_event_type,
                    payload={
                        **requested_payload,
                        "request_id": request_id,
                        "subject_sha256": subject_sha256,
                    },
                    created_at=now,
                ),
            ]
            for event in events:
                self._insert_event_locked(event)
        return record, events

    def reopen_decision(
        self,
        run: RunRecord,
        request_id: str,
        *,
        previous_state: RunState,
        requested_event_type: EventType,
        requested_payload: dict[str, Any],
    ) -> tuple[DecisionRequest, list[RunEvent]]:
        """Re-expose a persisted pending/accepted request after explicit resume."""

        now = utc_now()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM decision_requests WHERE run_id = ? AND request_id = ?",
                (run.id, request_id),
            ).fetchone()
            if row is None:
                raise DecisionConflictError("Decision request is no longer available")
            record = self._row_to_decision(row)
            if record.status not in {DecisionStatus.PENDING, DecisionStatus.ACCEPTED}:
                raise DecisionConflictError("Decision request is no longer active")
            current = self._connection.execute(
                "SELECT state FROM runs WHERE id = ?", (run.id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"Run not found: {run.id}")
            if current["state"] != previous_state.value:
                raise DecisionConflictError("Run state changed before decision recovery")
            run.updated_at = now
            self._update_run_locked(run, expected_state=previous_state)
            first_seq = self._next_event_seq_locked(run.id)
            events = [
                RunEvent(
                    run_id=run.id,
                    seq=first_seq,
                    type=EventType.STATE_CHANGED,
                    payload={
                        "state": run.state.value,
                        "previous": previous_state.value,
                        "cause": "decision_reopened",
                    },
                    created_at=now,
                ),
                RunEvent(
                    run_id=run.id,
                    seq=first_seq + 1,
                    type=requested_event_type,
                    payload={
                        **requested_payload,
                        "request_id": request_id,
                        "subject_sha256": record.subject_sha256,
                        "resumed": True,
                    },
                    created_at=now,
                ),
            ]
            for event in events:
                self._insert_event_locked(event)
        return record, events

    def accept_decision(
        self,
        run_id: str,
        request_id: str,
        kind: DecisionKind,
        payload: dict[str, Any],
    ) -> DecisionRequest:
        """Persist a decision before HTTP acknowledges it; exact retries are idempotent."""

        payload_sha256 = decision_payload_sha256(payload)
        rendered = self._json_dumps(payload, sort_keys=True)
        now = utc_now()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM decision_requests WHERE run_id = ? AND request_id = ?",
                (run_id, request_id),
            ).fetchone()
            if row is None:
                raise DecisionConflictError("Decision request is unknown or expired")
            record = self._row_to_decision(row)
            if record.kind is not kind:
                raise DecisionConflictError("Decision request kind does not match")
            if record.status is DecisionStatus.PENDING:
                cursor = self._connection.execute(
                    """
                    UPDATE decision_requests
                    SET status = ?, payload_json = ?, payload_sha256 = ?, accepted_at = ?
                    WHERE run_id = ? AND request_id = ? AND status = ?
                    """,
                    (
                        DecisionStatus.ACCEPTED.value,
                        rendered,
                        payload_sha256,
                        now.isoformat(),
                        run_id,
                        request_id,
                        DecisionStatus.PENDING.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise DecisionConflictError("Decision request changed while being accepted")
            elif record.status in {
                DecisionStatus.ACCEPTED,
                DecisionStatus.CONSUMED,
                DecisionStatus.UNCERTAIN,
            }:
                if record.payload_sha256 != payload_sha256:
                    raise DecisionConflictError(
                        "A different response was already submitted for this decision request"
                    )
                return record
            else:
                raise DecisionConflictError("Decision request was abandoned")
            row = self._connection.execute(
                "SELECT * FROM decision_requests WHERE run_id = ? AND request_id = ?",
                (run_id, request_id),
            ).fetchone()
            assert row is not None
            return self._row_to_decision(row)

    def consume_decision(
        self,
        run: RunRecord,
        request_id: str,
        kind: DecisionKind,
        *,
        previous_state: RunState,
        resolved_event_type: EventType,
        resolved_payload: dict[str, Any],
        action_call_payload: dict[str, Any] | None = None,
        completed_tool_payload: dict[str, Any] | None = None,
    ) -> tuple[DecisionRequest, list[RunEvent]]:
        """Atomically apply an accepted response, close its inbox row, and emit evidence."""

        if (
            action_call_payload is not None or completed_tool_payload is not None
        ) and kind is not DecisionKind.ACTION:
            raise ValueError("Only an action decision can persist tool execution evidence")
        if action_call_payload is not None and completed_tool_payload is not None:
            raise ValueError("An action cannot start and complete deterministically together")
        now = utc_now()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM decision_requests WHERE run_id = ? AND request_id = ?",
                (run.id, request_id),
            ).fetchone()
            if row is None:
                raise DecisionConflictError("Decision request is no longer available")
            record = self._row_to_decision(row)
            if record.kind is not kind or record.status is not DecisionStatus.ACCEPTED:
                raise DecisionConflictError("Decision request is not accepted and consumable")
            current = self._connection.execute(
                "SELECT state FROM runs WHERE id = ?", (run.id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"Run not found: {run.id}")
            if current["state"] != previous_state.value:
                raise DecisionConflictError("Run state changed before decision consumption")
            cursor = self._connection.execute(
                """
                UPDATE decision_requests
                SET status = ?, consumed_at = ?, execution_started_at = ?
                WHERE run_id = ? AND request_id = ? AND status = ?
                """,
                (
                    DecisionStatus.CONSUMED.value,
                    now.isoformat(),
                    now.isoformat() if action_call_payload is not None else None,
                    run.id,
                    request_id,
                    DecisionStatus.ACCEPTED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise DecisionConflictError("Decision request changed before consumption")
            run.updated_at = now
            self._update_run_locked(run, expected_state=previous_state)
            first_seq = self._next_event_seq_locked(run.id)
            events = [
                RunEvent(
                    run_id=run.id,
                    seq=first_seq,
                    type=resolved_event_type,
                    payload={
                        **resolved_payload,
                        "request_id": request_id,
                        "subject_sha256": record.subject_sha256,
                    },
                    created_at=now,
                ),
                RunEvent(
                    run_id=run.id,
                    seq=first_seq + 1,
                    type=EventType.STATE_CHANGED,
                    payload={
                        "state": run.state.value,
                        "previous": previous_state.value,
                    },
                    created_at=now,
                ),
            ]
            if action_call_payload is not None:
                events.append(
                    RunEvent(
                        run_id=run.id,
                        seq=first_seq + 2,
                        type=EventType.TOOL_STARTED,
                        payload={
                            **action_call_payload,
                            "approval_request_id": request_id,
                        },
                        created_at=now,
                    )
                )
            if completed_tool_payload is not None:
                events.append(
                    RunEvent(
                        run_id=run.id,
                        seq=first_seq + 2,
                        type=EventType.TOOL_COMPLETED,
                        payload=completed_tool_payload,
                        created_at=now,
                    )
                )
            for event in events:
                self._insert_event_locked(event)
            row = self._connection.execute(
                "SELECT * FROM decision_requests WHERE run_id = ? AND request_id = ?",
                (run.id, request_id),
            ).fetchone()
            assert row is not None
            return self._row_to_decision(row), events

    def abandon_decision(
        self,
        run: RunRecord,
        request_id: str,
        *,
        event_type: EventType,
        event_payload: dict[str, Any],
        include_request_id: bool = True,
    ) -> tuple[DecisionRequest, RunEvent]:
        """Atomically clear a decision's run subject, close its inbox row, and audit it."""

        now = utc_now()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM decision_requests WHERE run_id = ? AND request_id = ?",
                (run.id, request_id),
            ).fetchone()
            if row is None:
                raise DecisionConflictError("Decision request is no longer abandonable")
            record = self._row_to_decision(row)
            if record.status not in {DecisionStatus.PENDING, DecisionStatus.ACCEPTED}:
                raise DecisionConflictError("Decision request is no longer abandonable")
            cursor = self._connection.execute(
                """
                UPDATE decision_requests SET status = ?, consumed_at = ?
                WHERE run_id = ? AND request_id = ? AND status IN (?, ?)
                """,
                (
                    DecisionStatus.ABANDONED.value,
                    now.isoformat(),
                    run.id,
                    request_id,
                    DecisionStatus.PENDING.value,
                    DecisionStatus.ACCEPTED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise DecisionConflictError("Decision request is no longer abandonable")
            run.updated_at = now
            self._update_run_locked(run, expected_state=run.state)
            event = RunEvent(
                run_id=run.id,
                seq=self._next_event_seq_locked(run.id),
                type=event_type,
                payload={
                    **event_payload,
                    "kind": record.kind.value,
                    **({"request_id": request_id} if include_request_id else {}),
                },
                created_at=now,
            )
            self._insert_event_locked(event)
            row = self._connection.execute(
                "SELECT * FROM decision_requests WHERE run_id = ? AND request_id = ?",
                (run.id, request_id),
            ).fetchone()
            assert row is not None
            return self._row_to_decision(row), event

    def mark_action_uncertain(self, run_id: str, request_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE decision_requests SET status = ?
                WHERE run_id = ? AND request_id = ? AND kind = ? AND status = ?
                  AND execution_started_at IS NOT NULL
                """,
                (
                    DecisionStatus.UNCERTAIN.value,
                    run_id,
                    request_id,
                    DecisionKind.ACTION.value,
                    DecisionStatus.CONSUMED.value,
                ),
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
                    self._json_dumps(event.payload),
                    event.created_at.isoformat(),
                ),
            )
        return event

    def list_open_assistant_output_streams(self, run_id: str) -> list[dict[str, Any]]:
        """Return durable stream generations that were neither committed nor aborted."""

        with self._lock:
            return list(self._open_assistant_output_streams_locked(run_id).values())

    def abort_open_assistant_output_streams(
        self,
        run_id: str,
        *,
        status: str,
        reason: str,
        stream_id: str | None = None,
    ) -> list[RunEvent]:
        """Atomically close still-provisional stream generations, if any."""

        with self._lock, self._connection:
            return self._abort_open_assistant_output_streams_locked(
                run_id,
                status=status,
                reason=reason,
                stream_id=stream_id,
            )

    def commit_interruption(
        self,
        run: RunRecord,
        *,
        previous_state: RunState,
        stream_status: str,
        stream_reason: str,
        state_payload: dict[str, Any] | None = None,
        error_payload: dict[str, Any] | None = None,
    ) -> list[RunEvent]:
        """Atomically close provisional output and publish a recoverable interruption."""

        if run.state is not RunState.INTERRUPTED:
            raise ValueError("Atomic interruption commit requires an interrupted RunRecord")
        persisted_state_payload = state_payload or {
            "state": RunState.INTERRUPTED.value,
            "previous": previous_state.value,
        }
        if persisted_state_payload.get("state") != RunState.INTERRUPTED.value:
            raise ValueError("Interruption state payload does not match the RunRecord")
        if persisted_state_payload.get("previous") != previous_state.value:
            raise ValueError("Interruption previous state payload does not match the state guard")

        now = utc_now()
        with self._lock, self._connection:
            current = self._connection.execute(
                "SELECT state FROM runs WHERE id = ?", (run.id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"Run not found: {run.id}")
            if current["state"] != previous_state.value:
                raise RuntimeError("Run state changed before its atomic interruption commit")
            events = self._abort_open_assistant_output_streams_locked(
                run.id,
                status=stream_status,
                reason=stream_reason,
                created_at=now,
            )
            run.updated_at = now
            self._update_run_locked(run, expected_state=previous_state)
            sequence = self._next_event_seq_locked(run.id)
            if error_payload is not None:
                error_event = RunEvent(
                    run_id=run.id,
                    seq=sequence,
                    type=EventType.ERROR,
                    payload=error_payload,
                    created_at=now,
                )
                self._insert_event_locked(error_event)
                events.append(error_event)
                sequence += 1
            state_event = RunEvent(
                run_id=run.id,
                seq=sequence,
                type=EventType.STATE_CHANGED,
                payload=persisted_state_payload,
                created_at=now,
            )
            self._insert_event_locked(state_event)
            events.append(state_event)
        return events

    def commit_terminal_turn(
        self,
        run: RunRecord,
        *,
        previous_state: RunState,
        turn_payload: dict[str, Any],
        completion_payload: dict[str, Any],
    ) -> list[RunEvent]:
        """Atomically publish an answered, failed, or cancelled terminal turn."""

        expected_outcomes = {
            RunState.ANSWERED: "answered",
            RunState.FAILED: "failed",
            RunState.CANCELLED: "cancelled",
        }
        expected_outcome = expected_outcomes.get(run.state)
        if expected_outcome is None:
            raise ValueError("Atomic terminal commit requires answered, failed, or cancelled")
        if not run.turns or run.turns[-1].outcome != expected_outcome:
            raise ValueError("Atomic terminal commit requires a matching closed turn")
        if turn_payload.get("outcome") != expected_outcome:
            raise ValueError("Terminal turn payload does not match the RunRecord")
        if completion_payload.get("state") != run.state.value:
            raise ValueError("Terminal completion payload does not match the RunRecord")

        with self._lock, self._connection:
            current = self._connection.execute(
                "SELECT state FROM runs WHERE id = ?", (run.id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"Run not found: {run.id}")
            if current["state"] != previous_state.value:
                raise RuntimeError("Run state changed before its atomic terminal commit")
            now = utc_now()
            first_seq = self._next_event_seq_locked(run.id)
            terminal_events = [
                RunEvent(
                    run_id=run.id,
                    seq=first_seq,
                    type=EventType.STATE_CHANGED,
                    payload={
                        "state": run.state.value,
                        "previous": previous_state.value,
                    },
                    created_at=now,
                ),
                RunEvent(
                    run_id=run.id,
                    seq=first_seq + 1,
                    type=EventType.TURN_COMPLETED,
                    payload=turn_payload,
                    created_at=now,
                ),
                RunEvent(
                    run_id=run.id,
                    seq=first_seq + 2,
                    type=EventType.RUN_COMPLETED,
                    payload=completion_payload,
                    created_at=now,
                ),
            ]
            run.updated_at = now
            self._update_run_locked(run, expected_state=previous_state)
            for event in terminal_events:
                self._insert_event_locked(event)
        return terminal_events

    def commit_credential_conflict_cancellation(self, run_id: str) -> list[RunEvent]:
        """Atomically stop an interrupted run whose private context is no longer writable.

        A provider credential can be rotated to a value that already exists in an interrupted
        turn's formerly ordinary model context. The normal cancellation path deliberately cannot
        serialize that row under the new credential guard. This narrow recovery transaction
        discards model-facing subjects without reading them back through ordinary persistence,
        abandons any active durable decision, and closes the run. Workspace snapshots and the
        immutable workspace-instruction snapshot table are intentionally untouched.
        """

        preferred_time = utc_now()
        safe_task = CREDENTIAL_CONFLICT_TASK
        safe_summary = CREDENTIAL_CONFLICT_SUMMARY
        with self._lock, self._connection:
            now = self._credential_safe_recovery_time_locked(preferred_time)
            row = self._connection.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Run not found: {run_id}")
            if row["state"] != RunState.INTERRUPTED.value:
                raise RuntimeError(
                    "Credential-conflict cancellation requires an interrupted run"
                )

            turn_index = self._recover_turn_index_locked(row, run_id)
            try:
                mode = InteractionMode(str(row["mode"]))
            except ValueError:
                mode = InteractionMode.AGENT
            try:
                approval_mode = ApprovalMode(str(row["approval_mode"]))
            except ValueError:
                approval_mode = ApprovalMode.AUTOMATIC
            try:
                reasoning_effort = ReasoningEffort(str(row["reasoning_effort"]))
            except ValueError:
                reasoning_effort = ReasoningEffort.AUTO
            safe_turn = ConversationTurn(
                index=turn_index,
                request=safe_task,
                mode=mode,
                approval_mode=approval_mode,
                reasoning_effort=reasoning_effort,
                outcome="cancelled",
                summary=safe_summary,
                started_at=now,
                completed_at=now,
            )

            active_decisions = self._connection.execute(
                """
                SELECT request_id, kind FROM decision_requests
                WHERE run_id = ? AND status IN (?, ?)
                ORDER BY created_at
                """,
                (
                    run_id,
                    DecisionStatus.PENDING.value,
                    DecisionStatus.ACCEPTED.value,
                ),
            ).fetchall()
            self._connection.execute(
                """
                UPDATE decision_requests
                SET status = ?, subject_sha256 = ?, payload_json = NULL,
                    payload_sha256 = NULL, consumed_at = ?
                WHERE run_id = ? AND status IN (?, ?)
                """,
                (
                    DecisionStatus.ABANDONED.value,
                    CREDENTIAL_CONFLICT_DISCARDED_SUBJECT,
                    now.isoformat(),
                    run_id,
                    DecisionStatus.PENDING.value,
                    DecisionStatus.ACCEPTED.value,
                ),
            )

            empty_json = self._json_dumps([])
            cursor = self._connection.execute(
                """
                UPDATE runs SET
                    task = ?, state = ?, turns_json = ?,
                    plan_json = NULL, clarification_json = NULL,
                    pending_approval_json = NULL, verification_json = NULL,
                    plan_gate_json = NULL, messages_json = ?,
                    provider_reasoning_cleanup_pending = 1,
                    plan_approved = 0, interrupted_from = NULL,
                    error = NULL, created_at = ?, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (
                    safe_task,
                    RunState.CANCELLED.value,
                    self._json_dumps([safe_turn.model_dump(mode="json")]),
                    empty_json,
                    now.isoformat(),
                    now.isoformat(),
                    run_id,
                    RunState.INTERRUPTED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "Run state changed before credential-conflict cancellation"
                )

            sequence = self._next_event_seq_locked(run_id)
            events: list[RunEvent] = []
            if self._open_assistant_output_streams_locked(run_id):
                events.append(
                    RunEvent(
                        run_id=run_id,
                        seq=sequence,
                        type=EventType.ASSISTANT_OUTPUT_ABORTED,
                        payload={
                            "status": "cancelled",
                            "reason": CREDENTIAL_CONFLICT_CAUSE,
                            "all_open": True,
                        },
                        created_at=now,
                    )
                )
                sequence += 1
            events.extend(
                [
                    RunEvent(
                        run_id=run_id,
                        seq=sequence + offset,
                        type=EventType.DECISION_ABANDONED,
                        payload={
                            "kind": DecisionKind(str(decision["kind"])).value,
                            "cause": CREDENTIAL_CONFLICT_CAUSE,
                            "unsafe_subject_discarded": True,
                        },
                        created_at=now,
                    )
                    for offset, decision in enumerate(active_decisions)
                ]
            )
            sequence += len(active_decisions)
            events.extend(
                [
                    RunEvent(
                        run_id=run_id,
                        seq=sequence,
                        type=EventType.STATE_CHANGED,
                        payload={
                            "state": RunState.CANCELLED.value,
                            "previous": RunState.INTERRUPTED.value,
                            "cause": CREDENTIAL_CONFLICT_CAUSE,
                        },
                        created_at=now,
                    ),
                    RunEvent(
                        run_id=run_id,
                        seq=sequence + 1,
                        type=EventType.TURN_COMPLETED,
                        payload={
                            "index": turn_index,
                            "outcome": "cancelled",
                            "summary": safe_summary,
                            "changed_files": [],
                            "approval_mode": approval_mode.value,
                            "reasoning_effort": reasoning_effort.value,
                        },
                        created_at=now,
                    ),
                    RunEvent(
                        run_id=run_id,
                        seq=sequence + 2,
                        type=EventType.RUN_COMPLETED,
                        payload={
                            "state": RunState.CANCELLED.value,
                            "cause": CREDENTIAL_CONFLICT_CAUSE,
                        },
                        created_at=now,
                    ),
                ]
            )
            for event in events:
                self._insert_event_locked(event)
        return events

    def _credential_safe_recovery_time_locked(self, preferred: datetime) -> datetime:
        """Choose one timestamp that cannot reproduce any registered credential."""

        candidates = [preferred]
        with self._lock:
            credential_count = len(self._guarded_credentials)
        baseline = datetime(2000, 1, 1, 0, 0, 0, 123456, tzinfo=UTC)
        for offset in range(credential_count * 8 + 64):
            candidates.append(
                baseline
                + timedelta(
                    days=offset,
                    seconds=(offset * 7_919) % 86_400,
                    microseconds=(offset * 104_729) % 1_000_000,
                )
            )
        for candidate in candidates:
            try:
                self._assert_safe_text(candidate.isoformat())
            except CredentialPersistenceError:
                continue
            return candidate
        raise CredentialPersistenceError(
            "Credential-safe cancellation timestamp could not be constructed"
        )

    def finish_credential_conflict_cleanup(self, run_id: str) -> None:
        """Clear the recovery checkpoint marker after the old row bytes leave the WAL."""

        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE runs SET provider_reasoning_cleanup_pending = 0
                WHERE id = ? AND state = ?
                """,
                (run_id, RunState.CANCELLED.value),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "Credential-conflict cleanup requires a cancelled run"
                )

    def commit_success(
        self,
        run: RunRecord,
        *,
        previous_state: RunState,
        turn_payload: dict[str, Any],
        completion_payload: dict[str, Any],
        proof_factory: Callable[[list[RunEvent]], ProofPack],
    ) -> tuple[list[RunEvent], ProofPack]:
        """Atomically publish a closed successful turn, its events, and immutable proof."""

        if run.state is not RunState.SUCCEEDED:
            raise ValueError("Atomic success commit requires a successful RunRecord")
        if not run.turns or run.turns[-1].outcome != "succeeded":
            raise ValueError("Atomic success commit requires a closed successful turn")
        with self._lock, self._connection:
            current = self._connection.execute(
                "SELECT state FROM runs WHERE id = ?", (run.id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"Run not found: {run.id}")
            if current["state"] != previous_state.value:
                raise RuntimeError(
                    "Run state changed before its atomic success commit"
                )
            existing_events = self.get_events(run.id)
            now = utc_now()
            first_seq = existing_events[-1].seq + 1 if existing_events else 1
            terminal_events = [
                RunEvent(
                    run_id=run.id,
                    seq=first_seq,
                    type=EventType.STATE_CHANGED,
                    payload={
                        "state": RunState.SUCCEEDED.value,
                        "previous": previous_state.value,
                    },
                    created_at=now,
                ),
                RunEvent(
                    run_id=run.id,
                    seq=first_seq + 1,
                    type=EventType.TURN_COMPLETED,
                    payload=turn_payload,
                    created_at=now,
                ),
                RunEvent(
                    run_id=run.id,
                    seq=first_seq + 2,
                    type=EventType.RUN_COMPLETED,
                    payload=completion_payload,
                    created_at=now,
                ),
            ]
            run.updated_at = now
            self._update_run_locked(run, expected_state=previous_state)
            for event in terminal_events:
                self._insert_event_locked(event)
            pack = proof_factory([*existing_events, *terminal_events])
            if pack.turn_index != run.turns[-1].index:
                raise ValueError(
                    "Proof Pack turn does not match the atomically completed turn"
                )
            if pack.event_through_seq != terminal_events[-1].seq:
                raise ValueError("Proof Pack does not end at the committed success event")
            stored = self._save_proof_pack_if_absent_locked(
                run.id, pack.turn_index, pack
            )
            if stored.artifact_sha256 != pack.artifact_sha256:
                raise ValueError(
                    "A different Proof Pack already exists for the completed turn"
                )
            self._connection.execute(
                "DELETE FROM proof_backfill_candidates WHERE run_id = ?", (run.id,)
            )
        return terminal_events, stored

    def commit_rollback(
        self,
        run: RunRecord,
        *,
        previous_state: RunState,
        rollback_payload: dict[str, Any],
        turn_payload: dict[str, Any] | None = None,
    ) -> list[RunEvent]:
        """Atomically publish the rolled-back state and its replayable result."""

        if run.state is not RunState.ROLLED_BACK:
            raise ValueError("Atomic rollback commit requires a rolled-back RunRecord")
        now = utc_now()
        with self._lock, self._connection:
            current = self._connection.execute(
                "SELECT state FROM runs WHERE id = ?", (run.id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"Run not found: {run.id}")
            if current["state"] != previous_state.value:
                raise RuntimeError("Run state changed before its atomic rollback commit")
            first_seq = self._next_event_seq_locked(run.id)
            events = [
                RunEvent(
                    run_id=run.id,
                    seq=first_seq,
                    type=EventType.STATE_CHANGED,
                    payload={
                        "state": RunState.ROLLED_BACK.value,
                        "previous": previous_state.value,
                    },
                    created_at=now,
                ),
            ]
            if turn_payload is not None:
                events.append(
                    RunEvent(
                        run_id=run.id,
                        seq=first_seq + 1,
                        type=EventType.TURN_COMPLETED,
                        payload=turn_payload,
                        created_at=now,
                    )
                )
            events.append(
                RunEvent(
                    run_id=run.id,
                    seq=first_seq + len(events),
                    type=EventType.ROLLBACK_COMPLETED,
                    payload=rollback_payload,
                    created_at=now,
                )
            )
            run.updated_at = now
            self._update_run_locked(run, expected_state=previous_state)
            for event in events:
                self._insert_event_locked(event)
        return events

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

    def save_proof_pack_if_absent(
        self, run_id: str, turn_index: int, pack: ProofPack
    ) -> ProofPack:
        """Persist one immutable successful-turn proof and return the stored value."""

        self._validate_proof_pack_key(pack, run_id, turn_index)
        with self._lock, self._connection:
            stored = self._save_proof_pack_if_absent_locked(run_id, turn_index, pack)
            self._connection.execute(
                "DELETE FROM proof_backfill_candidates WHERE run_id = ?", (run_id,)
            )
            return stored

    def get_proof_pack(
        self, run_id: str, turn_index: int | None = None
    ) -> ProofPack | None:
        """Load and validate an exact or latest immutable successful-turn proof."""

        if turn_index is not None and turn_index < 1:
            raise ValueError("Proof Pack turn index must be positive")
        with self._lock:
            if turn_index is None:
                row = self._connection.execute(
                    """
                    SELECT run_id, turn_index, proof_json FROM proof_packs
                    WHERE run_id = ? ORDER BY turn_index DESC LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
            else:
                row = self._connection.execute(
                    """
                    SELECT run_id, turn_index, proof_json FROM proof_packs
                    WHERE run_id = ? AND turn_index = ?
                    """,
                    (run_id, turn_index),
                ).fetchone()
        if row is None:
            return None
        pack = ProofPack.model_validate_json(row["proof_json"])
        self._validate_proof_pack_key(pack, str(row["run_id"]), int(row["turn_index"]))
        return pack

    def list_proof_pack_turn_indexes(self, run_id: str) -> list[int]:
        """List frozen rows cheaply; exact reads validate the signed public artifact."""

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT turn_index FROM proof_packs
                WHERE run_id = ? ORDER BY turn_index
                """,
                (run_id,),
            ).fetchall()
        return [int(row["turn_index"]) for row in rows]

    def is_proof_backfill_candidate(self, run_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM proof_backfill_candidates WHERE run_id = ?", (run_id,)
            ).fetchone()
        return row is not None

    def list_proof_backfill_candidate_ids(self) -> list[str]:
        """Return legacy current-success rows that can still be frozen faithfully."""

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT proof_backfill_candidates.run_id
                FROM proof_backfill_candidates
                JOIN runs ON runs.id = proof_backfill_candidates.run_id
                WHERE runs.state = ?
                ORDER BY proof_backfill_candidates.run_id
                """,
                (RunState.SUCCEEDED.value,),
            ).fetchall()
        return [str(row["run_id"]) for row in rows]

    def _save_proof_pack_if_absent_locked(
        self, run_id: str, turn_index: int, pack: ProofPack
    ) -> ProofPack:
        pack = ProofPack.model_validate(pack.model_dump(mode="json"))
        self._validate_proof_pack_key(pack, run_id, turn_index)
        rendered = self._json_dumps(pack.model_dump(mode="json"))
        self._connection.execute(
            """
            INSERT OR IGNORE INTO proof_packs(
                run_id, turn_index, proof_json, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (run_id, turn_index, rendered, utc_now().isoformat()),
        )
        row = self._connection.execute(
            """
            SELECT run_id, turn_index, proof_json FROM proof_packs
            WHERE run_id = ? AND turn_index = ?
            """,
            (run_id, turn_index),
        ).fetchone()
        assert row is not None
        stored = ProofPack.model_validate_json(row["proof_json"])
        self._validate_proof_pack_key(
            stored, str(row["run_id"]), int(row["turn_index"])
        )
        return stored

    @staticmethod
    def _validate_proof_pack_key(
        pack: ProofPack, run_id: str, turn_index: int
    ) -> None:
        if turn_index < 1:
            raise ValueError("Proof Pack turn index must be positive")
        if pack.run_id != run_id:
            raise ValueError("Proof Pack run id does not match its storage key")
        if pack.turn_index != turn_index:
            raise ValueError("Proof Pack turn index does not match its storage key")
        if pack.state is not RunState.SUCCEEDED:
            raise ValueError("Only successful runs can be frozen as Proof Packs")
        if pack.event_count != pack.event_through_seq:
            raise ValueError("Proof Pack event boundary is inconsistent")

    def save_snapshot_if_absent(self, snapshot: SnapshotRecord) -> bool:
        self._assert_safe_value(
            {
                "run_id": snapshot.run_id,
                "path": snapshot.path,
                "content": snapshot.content,
                "original_hash": snapshot.original_hash,
                "last_agent_hash": snapshot.last_agent_hash,
            }
        )
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

    def _run_values(self, run: RunRecord) -> tuple[Any, ...]:
        self._assert_safe_value(
            {
                "id": run.id,
                "task": run.task,
                "workspace": run.workspace,
                "project_id": run.project_id,
                "error": run.error,
            }
        )
        return (
            run.id,
            run.task,
            run.workspace,
            run.project_id,
            run.state.value,
            run.mode.value,
            run.approval_mode.value,
            run.reasoning_effort.value,
            self._json_dumps([turn.model_dump(mode="json") for turn in run.turns]),
            int(run.verifier_enabled),
            self._dump_model(run.plan),
            self._dump_model(run.clarification),
            self._dump_model(run.pending_approval),
            self._dump_model(run.verification),
            self._dump_model(run.plan_gate),
            self._json_dumps(run.messages),
            int(run.provider_reasoning_cleanup_pending),
            int(run.plan_approved),
            run.interrupted_from.value if run.interrupted_from else None,
            run.step_count,
            run.repair_cycles,
            run.context_limit,
            run.error,
            run.created_at.isoformat(),
            run.updated_at.isoformat(),
        )

    def _dump_model(self, value: BaseModel | None) -> str | None:
        if value is None:
            return None
        return self._json_dumps(value.model_dump(mode="json"))

    def _update_run_locked(
        self, run: RunRecord, *, expected_state: RunState | None = None
    ) -> None:
        values = self._run_values(run)
        state_guard = " AND state = ?" if expected_state is not None else ""
        parameters = (
            (*values[1:], values[0], expected_state.value)
            if expected_state is not None
            else (*values[1:], values[0])
        )
        cursor = self._connection.execute(
            f"""
            UPDATE runs SET
                task = ?, workspace = ?, project_id = ?, state = ?, mode = ?,
                approval_mode = ?, reasoning_effort = ?, turns_json = ?, verifier_enabled = ?,
                plan_json = ?, clarification_json = ?, pending_approval_json = ?,
                verification_json = ?, plan_gate_json = ?, messages_json = ?,
                provider_reasoning_cleanup_pending = ?, plan_approved = ?,
                interrupted_from = ?, step_count = ?,
                repair_cycles = ?, context_limit = ?, error = ?, created_at = ?, updated_at = ?
            WHERE id = ?{state_guard}
            """,
            parameters,
        )
        if cursor.rowcount != 1:
            if expected_state is None:
                raise KeyError(f"Run not found: {run.id}")
            raise RuntimeError(f"Run update lost its state guard: {run.id}")

    def _recover_turn_index_locked(self, row: sqlite3.Row, run_id: str) -> int:
        """Recover only the non-secret numeric turn identity from an unsafe run row."""

        try:
            turns = json.loads(str(row["turns_json"]))
        except (TypeError, ValueError):
            turns = []
        if isinstance(turns, list):
            for turn in reversed(turns):
                if isinstance(turn, dict):
                    index = turn.get("index")
                    if isinstance(index, int) and not isinstance(index, bool) and index >= 1:
                        return index
        snapshot = self._connection.execute(
            """
            SELECT MAX(turn_index) AS turn_index
            FROM workspace_instruction_snapshots WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if snapshot is not None:
            index = snapshot["turn_index"]
            if isinstance(index, int) and index >= 1:
                return index
        return 1

    def _open_assistant_output_streams_locked(
        self, run_id: str
    ) -> dict[str, dict[str, Any]]:
        rows = self._connection.execute(
            """
            SELECT type, payload_json FROM events
            WHERE run_id = ? AND type IN (?, ?, ?)
            ORDER BY seq
            """,
            (
                run_id,
                EventType.ASSISTANT_OUTPUT_STARTED.value,
                EventType.ASSISTANT_OUTPUT_ABORTED.value,
                EventType.TURN_COMPLETED.value,
            ),
        ).fetchall()
        open_streams: dict[str, dict[str, Any]] = {}
        for row in rows:
            payload = json.loads(row["payload_json"])
            if not isinstance(payload, dict):
                continue
            event_type = EventType(row["type"])
            if event_type is EventType.ASSISTANT_OUTPUT_STARTED:
                candidate = payload.get("stream_id")
                if isinstance(candidate, str) and candidate:
                    open_streams[candidate] = payload
            elif event_type is EventType.ASSISTANT_OUTPUT_ABORTED:
                if payload.get("all_open") is True:
                    open_streams.clear()
                    continue
                candidate = payload.get("stream_id")
                if isinstance(candidate, str):
                    open_streams.pop(candidate, None)
            else:
                committed = payload.get("final_stream_id")
                if isinstance(committed, str):
                    open_streams.pop(committed, None)
        return open_streams

    def _abort_open_assistant_output_streams_locked(
        self,
        run_id: str,
        *,
        status: str,
        reason: str,
        stream_id: str | None = None,
        created_at: datetime | None = None,
    ) -> list[RunEvent]:
        open_streams = self._open_assistant_output_streams_locked(run_id)
        if stream_id is not None:
            payload = open_streams.get(stream_id)
            open_streams = {stream_id: payload} if payload is not None else {}
        if not open_streams:
            return []
        now = created_at or utc_now()
        first_seq = self._next_event_seq_locked(run_id)
        events = [
            RunEvent(
                run_id=run_id,
                seq=first_seq + offset,
                type=EventType.ASSISTANT_OUTPUT_ABORTED,
                payload={
                    **payload,
                    "status": status,
                    "reason": reason,
                },
                created_at=now,
            )
            for offset, payload in enumerate(open_streams.values())
        ]
        for event in events:
            self._insert_event_locked(event)
        return events

    def _insert_event_locked(self, event: RunEvent) -> None:
        self._connection.execute(
            """
            INSERT INTO events(run_id, seq, type, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                event.run_id,
                event.seq,
                event.type.value,
                self._json_dumps(event.payload),
                event.created_at.isoformat(),
            ),
        )

    def _insert_workspace_instruction_snapshot_locked(
        self,
        run_id: str,
        turn_index: int,
        snapshot: WorkspaceInstructionSnapshot,
    ) -> None:
        if turn_index < 1:
            raise ValueError("Workspace instruction turn index must be positive")
        self._connection.execute(
            """
            INSERT INTO workspace_instruction_snapshots(
                run_id, turn_index, schema_version, snapshot_sha256,
                snapshot_json, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                turn_index,
                snapshot.schema_version,
                snapshot.snapshot_sha256,
                self._json_dumps(snapshot.model_dump(mode="json"), sort_keys=True),
                snapshot.captured_at.isoformat(),
            ),
        )

    def _insert_decision_locked(self, decision: DecisionRequest) -> None:
        self._connection.execute(
            """
            INSERT INTO decision_requests(
                run_id, request_id, kind, turn_index, subject_sha256, status,
                payload_json, payload_sha256, created_at, accepted_at, consumed_at,
                execution_started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision.run_id,
                decision.request_id,
                decision.kind.value,
                decision.turn_index,
                decision.subject_sha256,
                decision.status.value,
                (
                    self._json_dumps(decision.payload, sort_keys=True)
                    if decision.payload is not None
                    else None
                ),
                decision.payload_sha256,
                decision.created_at.isoformat(),
                decision.accepted_at.isoformat() if decision.accepted_at else None,
                decision.consumed_at.isoformat() if decision.consumed_at else None,
                (
                    decision.execution_started_at.isoformat()
                    if decision.execution_started_at
                    else None
                ),
            ),
        )

    def _next_event_seq_locked(self, run_id: str) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row is not None
        return int(row["seq"])

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=row["id"],
            task=row["task"],
            workspace=row["workspace"],
            project_id=row["project_id"],
            state=RunState(row["state"]),
            mode=InteractionMode(row["mode"]),
            approval_mode=ApprovalMode(row["approval_mode"]),
            reasoning_effort=ReasoningEffort(row["reasoning_effort"]),
            turns=json.loads(row["turns_json"]),
            verifier_enabled=bool(row["verifier_enabled"]),
            plan=_load_model(TaskPlan, row["plan_json"]),
            clarification=_load_model(ClarificationRequest, row["clarification_json"]),
            pending_approval=_load_model(ApprovalRequest, row["pending_approval_json"]),
            verification=_load_model(VerificationReport, row["verification_json"]),
            plan_gate=_load_model(PlanGate, row["plan_gate_json"]),
            messages=json.loads(row["messages_json"]),
            provider_reasoning_cleanup_pending=bool(
                row["provider_reasoning_cleanup_pending"]
            ),
            plan_approved=bool(row["plan_approved"]),
            interrupted_from=(
                RunState(row["interrupted_from"]) if row["interrupted_from"] else None
            ),
            step_count=row["step_count"],
            repair_cycles=row["repair_cycles"],
            context_limit=row["context_limit"],
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

    @staticmethod
    def _row_to_decision(row: sqlite3.Row) -> DecisionRequest:
        return DecisionRequest(
            run_id=row["run_id"],
            request_id=row["request_id"],
            kind=DecisionKind(row["kind"]),
            turn_index=row["turn_index"],
            subject_sha256=row["subject_sha256"],
            status=DecisionStatus(row["status"]),
            payload=json.loads(row["payload_json"]) if row["payload_json"] else None,
            payload_sha256=row["payload_sha256"],
            created_at=datetime.fromisoformat(row["created_at"]).astimezone(UTC),
            accepted_at=(
                datetime.fromisoformat(row["accepted_at"]).astimezone(UTC)
                if row["accepted_at"]
                else None
            ),
            consumed_at=(
                datetime.fromisoformat(row["consumed_at"]).astimezone(UTC)
                if row["consumed_at"]
                else None
            ),
            execution_started_at=(
                datetime.fromisoformat(row["execution_started_at"]).astimezone(UTC)
                if row["execution_started_at"]
                else None
            ),
        )


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def decision_payload_sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _load_model[ModelT: BaseModel](model: type[ModelT], raw: str | None) -> ModelT | None:
    if raw is None:
        return None
    return model.model_validate_json(raw)

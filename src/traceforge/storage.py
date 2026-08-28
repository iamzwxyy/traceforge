from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from traceforge.models import (
    ApprovalMode,
    ApprovalRequest,
    ClarificationRequest,
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


class SecureCheckpointError(RuntimeError):
    """A WAL truncation needed for provider-private cleanup could not be confirmed."""


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

                CREATE TABLE IF NOT EXISTS proof_backfill_candidates (
                    run_id TEXT PRIMARY KEY,
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
            self._scrub_terminal_reasoning_rows()
            row = self._connection.execute(
                "SELECT 1 FROM runs WHERE provider_reasoning_cleanup_pending = 1 LIMIT 1"
            ).fetchone()
            return row is not None

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
                    (json.dumps(messages, ensure_ascii=False), row["id"]),
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
        pack = ProofPack.model_validate_json(pack.model_dump_json())
        self._validate_proof_pack_key(pack, run_id, turn_index)
        self._connection.execute(
            """
            INSERT OR IGNORE INTO proof_packs(
                run_id, turn_index, proof_json, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (run_id, turn_index, pack.model_dump_json(), utc_now().isoformat()),
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
            run.mode.value,
            run.approval_mode.value,
            run.reasoning_effort.value,
            json.dumps([turn.model_dump(mode="json") for turn in run.turns], ensure_ascii=False),
            int(run.verifier_enabled),
            _dump_model(run.plan),
            _dump_model(run.clarification),
            _dump_model(run.pending_approval),
            _dump_model(run.verification),
            _dump_model(run.plan_gate),
            json.dumps(run.messages, ensure_ascii=False),
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
                json.dumps(event.payload, ensure_ascii=False),
                event.created_at.isoformat(),
            ),
        )

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


def _dump_model(value: BaseModel | None) -> str | None:
    if value is None:
        return None
    return value.model_dump_json()


def _load_model[ModelT: BaseModel](model: type[ModelT], raw: str | None) -> ModelT | None:
    if raw is None:
        return None
    return model.model_validate_json(raw)

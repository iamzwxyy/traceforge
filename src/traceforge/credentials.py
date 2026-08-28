from __future__ import annotations

import hashlib
import json

from traceforge.streaming import boundary_safe_json_dumps, contains_secret_representation

PROVIDER_CREDENTIAL_MIN_BYTES = 12
PROVIDER_CREDENTIAL_MAX_BYTES = 16 * 1024

CREDENTIAL_CONFLICT_TASK = "Cancelled after a provider credential conflict"
CREDENTIAL_CONFLICT_SUMMARY = (
    "Stored conversation context was discarded because it conflicts with the current "
    "provider credential."
)
CREDENTIAL_CONFLICT_CAUSE = "credential_conflict_cancelled"
CREDENTIAL_CONFLICT_DISCARDED_SUBJECT = hashlib.sha256(
    b"traceforge:discarded-credential-conflict-subject"
).hexdigest()


_CREDENTIAL_CONFLICT_PROTOCOL_TEMPLATE = {
    "run": {
        "task": CREDENTIAL_CONFLICT_TASK,
        "state": "cancelled",
        "mode": "agent",
        "approval_mode": "automatic",
        "reasoning_effort": "auto",
        "turns": [
            {
                "index": 1,
                "request": CREDENTIAL_CONFLICT_TASK,
                "mode": "agent",
                "approval_mode": "automatic",
                "reasoning_effort": "auto",
                "outcome": "cancelled",
                "summary": CREDENTIAL_CONFLICT_SUMMARY,
                "summary_stream_id": None,
                "changed_files": [],
                "started_at": "recovery-time",
                "completed_at": "recovery-time",
            }
        ],
        "messages": [],
        "provider_reasoning_cleanup_pending": True,
        "plan_approved": False,
        "interrupted_from": None,
        "error": None,
    },
    "decision": {
        "status": "abandoned",
        "subject_sha256": CREDENTIAL_CONFLICT_DISCARDED_SUBJECT,
        "payload": None,
        "payload_sha256": None,
        "consumed_at": "recovery-time",
    },
    "events": [
        {
            "type": "assistant.output.aborted",
            "payload": {
                "status": "cancelled",
                "reason": CREDENTIAL_CONFLICT_CAUSE,
                "all_open": True,
            },
        },
        {
            "type": "decision.abandoned",
            "payload": {
                "kind": "clarification",
                "cause": CREDENTIAL_CONFLICT_CAUSE,
                "unsafe_subject_discarded": True,
            },
        },
        {
            "type": "state.changed",
            "payload": {
                "state": "cancelled",
                "previous": "interrupted",
                "cause": CREDENTIAL_CONFLICT_CAUSE,
            },
        },
        {
            "type": "turn.completed",
            "payload": {
                "index": 1,
                "outcome": "cancelled",
                "summary": CREDENTIAL_CONFLICT_SUMMARY,
                "changed_files": [],
                "approval_mode": "automatic",
                "reasoning_effort": "auto",
            },
        },
        {
            "type": "run.completed",
            "payload": {
                "state": "cancelled",
                "cause": CREDENTIAL_CONFLICT_CAUSE,
            },
        },
    ],
}
_CREDENTIAL_CONFLICT_PROTOCOL_SURFACE = "\n".join(
    (
        boundary_safe_json_dumps(_CREDENTIAL_CONFLICT_PROTOCOL_TEMPLATE),
        boundary_safe_json_dumps(
            _CREDENTIAL_CONFLICT_PROTOCOL_TEMPLATE,
            sort_keys=True,
        ),
        json.dumps(
            _CREDENTIAL_CONFLICT_PROTOCOL_TEMPLATE,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        json.dumps(
            _CREDENTIAL_CONFLICT_PROTOCOL_TEMPLATE,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )
)


def validate_provider_credential(value: str) -> bytes:
    """Validate one normalized credential against durable recovery invariants."""

    if not value or value != value.strip() or "\n" in value or "\r" in value:
        raise ValueError(
            "API key must contain exactly one non-empty line without surrounding whitespace"
        )
    encoded = value.encode("utf-8")
    if len(encoded) < PROVIDER_CREDENTIAL_MIN_BYTES:
        raise ValueError(
            f"API key must contain at least {PROVIDER_CREDENTIAL_MIN_BYTES} UTF-8 bytes"
        )
    if len(encoded) > PROVIDER_CREDENTIAL_MAX_BYTES:
        raise ValueError("API key must be smaller than 16 KiB")
    if contains_secret_representation(
        _CREDENTIAL_CONFLICT_PROTOCOL_SURFACE,
        api_key=value,
    ):
        raise ValueError(
            "API key conflicts with TraceForge's required recovery protocol; use a different "
            "credential"
        )
    return encoded
